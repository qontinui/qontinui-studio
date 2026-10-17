"""The weekly factory pulse (plan 2026-10-09-qontinui-marketing-studio-animated-
process-explainers, Phase 5).

One run produces one ISO week's pulse, the PREVIOUS week by default, under
``<root>/.studio/pulse/<YYYY-Www>/``:

1. **Export.** The product named by ``<root>/pulse.yaml`` has its build record
   read through the launch kit's doors (:mod:`qontinui_studio.launch.record`).
   With no product configured, or no record (404, unreachable, no
   credential), the outcome is ``no-export``: nothing is rendered and the
   review page says why. A pulse is never rendered from nothing.
2. **The week's numbers** come from the record, counted inside the week, and
   only when the record says the list is complete (a lower bound is said as
   "at least"; an unknown is left out, never 0).
3. **Narration.** A script is written from those numbers and voiced with
   ``studio-scenes narrate`` (Kokoro-FastAPI + a Whisper check). With no TTS
   service the pulse renders silent, and the gate blocks it (no audio, no
   captions, no speech check) — recorded, not hidden.
4. **Render.** ``studio-scenes compose factory-pulse --build-record`` is tried
   first. While replays are unavailable (it exits 5: ``fromBuildRecord`` needs
   qontinui-coord#3083) the pulse renders from the scenes' illustrative traces
   and says so: on every frame (the D2 label), on the closing card, in the
   narration, on the review page and in every draft. It never presents an
   illustrative clip as a replay.
5. **Gate.** ``studio-scenes gate`` decides. A failed gate blocks publication
   and records why (``blocked``): the clip stays in the pulse directory for
   the review page, and nothing is put in ``publish/`` or ``drafts/``. A
   record that is not a published public snapshot also blocks (its numbers
   could not be checked by a reader).
6. **Output.** ``ready``: ``publish/`` holds the clip, its captions, manifest
   and gate report, and ``drafts/`` one draft post per network. Every
   outcome writes ``review.html`` and ``pulse.json``.

Posting to social accounts and the final publish click are OPERATOR-OWNED
(sibling plan D7): this module posts nothing and publishes nothing beyond
files in the pulse directory.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from qontinui_studio.errors import ConfigError
from qontinui_studio.launch.posts import NETWORKS
from qontinui_studio.launch.product import LaunchProduct, load_product
from qontinui_studio.launch.record import (
    BuildRecord,
    BuildRecordClient,
    BuildRecordInvalid,
    FetchedRecord,
    NoBuildRecord,
    reason_text,
)

PULSE_VERSION = 1
COMPOSITION = "factory-pulse"
#: The narrated segments of the factory-pulse composition (scenes/src/compositions/factory-pulse.ts).
SEGMENTS = ("open", "lifecycle", "self-correction", "merge-train", "close")

Outcome = Literal["no-export", "blocked", "ready", "failed"]
Mode = Literal["illustrative", "replay"]

#: studio-scenes exit codes this module acts on.
EXIT_GATE_BLOCKED = 3
EXIT_REPLAY_UNAVAILABLE = 5
EXIT_NARRATION_UNAVAILABLE = 6


# ---- configuration -------------------------------------------------------------


@dataclass(frozen=True)
class PulseConfig:
    """``<root>/pulse.yaml``: which product's record drives the pulse, and the
    output shape. Absent file = no product configured (a ``no-export`` pulse)."""

    product_path: Path | None
    aspect: str = "16:9"


def load_pulse_config(root: Path) -> PulseConfig:
    path = root / "pulse.yaml"
    if not path.exists():
        return PulseConfig(product_path=None)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")
    unknown = set(raw) - {"product", "aspect"}
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    aspect = raw.get("aspect", "16:9")
    if aspect not in ("16:9", "9:16", "1:1"):
        raise ConfigError(f"{path}: aspect must be 16:9, 9:16 or 1:1")
    product = raw.get("product")
    if product is not None and not isinstance(product, str):
        raise ConfigError(f"{path}: product must be a path to products/<slug>.yaml")
    product_path = (root / product).resolve() if isinstance(product, str) else None
    return PulseConfig(product_path=product_path, aspect=aspect)


# ---- the week ------------------------------------------------------------------


@dataclass(frozen=True)
class Week:
    label: str  # "2026-W41"
    start: date  # Monday
    end: date  # Sunday

    @property
    def start_dt(self) -> datetime:
        return datetime(self.start.year, self.start.month, self.start.day, tzinfo=UTC)

    @property
    def end_dt(self) -> datetime:
        """Exclusive: the Monday after."""
        return self.start_dt + timedelta(days=7)

    def describe(self) -> str:
        return f"Week {self.start.isocalendar().week}, {self.start.isoformat()} to {self.end.isoformat()}"


_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")


def week_of(now: datetime, label: str | None = None) -> Week:
    """The ISO week ``label`` (``YYYY-Www``), or the week before ``now`` (UTC)."""
    if label is not None:
        m = _WEEK.match(label)
        if not m:
            raise ConfigError(f"--week must look like 2026-W41, got {label!r}")
        try:
            monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        except ValueError as e:
            raise ConfigError(f"--week {label}: {e}") from e
    else:
        today = now.astimezone(UTC).date()
        monday = today - timedelta(days=today.weekday() + 7)
    iso = monday.isocalendar()
    week = Week(f"{iso.year}-W{iso.week:02d}", monday, monday + timedelta(days=6))
    if week.end_dt > now.astimezone(UTC):
        raise ConfigError(f"week {week.label} has not ended yet; a pulse reports a finished week")
    return week


# ---- the week's numbers --------------------------------------------------------


@dataclass(frozen=True)
class WeekFact:
    key: str
    #: Plural phrase, e.g. "pull requests landed".
    label: str
    value: int | None
    lower_bound: bool = False
    unknown_reason: str | None = None
    #: Singular phrase, e.g. "pull request landed".
    singular: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None

    def phrase(self) -> str:
        """ "3 pull requests landed", "at least 1 plan shipped"."""
        count = f"at least {self.value}" if self.lower_bound else str(self.value)
        noun = (
            self.singular
            if self.value == 1 and not self.lower_bound and self.singular
            else self.label
        )
        return f"{count} {noun}"


def _parse_stamp(stamp: str | None) -> datetime | None:
    """An ISO timestamp with an offset; anything else is None (unknown)."""
    if stamp is None:
        return None
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _in_week(stamp: str | None, week: Week) -> bool | None:
    at = _parse_stamp(stamp)
    if at is None:
        return None
    return week.start_dt <= at < week.end_dt


def week_facts(record: BuildRecord, week: Week) -> list[WeekFact]:
    """Counts inside ``week``: PRs landed and plans shipped.

    - A list the record says it could not read is unknown (never 0).
    - The count is a LOWER BOUND ("at least") when the record could only
      partly establish the list; when an item that happened has no readable
      time (a PR whose ``landed_at`` the record marks unknown, a shipped plan
      with no ``shipped_at``, any unparseable or offset-less stamp); or when
      the record was generated before the week ended (a frozen snapshot
      cannot see what landed after it).
    - Otherwise an item with no time has not happened, and the count is exact.
    """
    generated = _parse_stamp(record.generated_at)
    stale = generated is None or generated < week.end_dt
    blind = generated is not None and generated < week.start_dt
    out: list[WeekFact] = []
    lists: tuple[tuple[str, str, str, str, list[tuple[str | None, bool]]], ...] = (
        (
            "prs_landed",
            "pull requests landed",
            "pull request landed",
            "prs",
            # A null landed_at is "not landed" unless the record marks the field unknown.
            [(p.landed_at, bool(record.unknown_codes("prs[].landed_at"))) for p in record.prs],
        ),
        (
            "work_units_shipped",
            "plans shipped",
            "plan shipped",
            "work_units",
            [
                (
                    u.shipped_at,
                    u.status == "shipped" or bool(record.unknown_codes("work_units[].shipped_at")),
                )
                for u in record.work_units
                # A unit demoted since it shipped is not shipped now.
                if u.status == "shipped" or u.shipped_at is None
            ],
        ),
    )
    for key, label, singular, path, items in lists:
        status = record.list_status(path)
        if blind:
            # Generated before the week began: it cannot see any of it.
            out.append(
                WeekFact(
                    key,
                    label,
                    None,
                    unknown_reason=f"the record was generated on {record.generated_at}, before the week began",
                    singular=singular,
                )
            )
            continue
        if status.state == "unknown":
            out.append(
                WeekFact(
                    key,
                    label,
                    None,
                    unknown_reason=reason_text(status.code or ""),
                    singular=singular,
                )
            )
            continue
        count = 0
        lower = stale or status.state == "lower_bound"
        for stamp, null_means_unknown in items:
            if stamp is None:
                lower = lower or null_means_unknown
                continue
            inside = _in_week(stamp, week)
            if inside is None:
                lower = True
            elif inside:
                count += 1
        out.append(WeekFact(key, label, count, lower_bound=lower, singular=singular))
    return out


# ---- narration script and drafts -----------------------------------------------


def narration_script(week: Week, facts: Sequence[WeekFact], mode: Mode) -> str:
    """The pulse's narration, one ``## <segment>`` section per segment. Only
    known numbers are spoken; the illustrative status is said aloud."""
    known = [f for f in facts if f.known]
    numbers = (
        " ".join(f"{f.phrase()}." for f in known)
        if known
        else "the build record could not establish this week's counts."
    )
    honesty = (
        "The scenes you see are illustrative. The numbers are from the build record."
        if mode == "illustrative"
        else "The scenes replay the factory's build record."
    )
    start = f"{week.start:%B} {week.start.day}"
    end = f"{week.end:%B} {week.end.day}"
    return "\n".join(
        [
            "# Factory pulse",
            "",
            "## open",
            f"This is the factory pulse for the week of {start} to {end}. {honesty}",
            "",
            "## lifecycle",
            f"This week, {numbers} A plan is drafted, vetted, built by an agent, and landed.",
            "",
            "## self-correction",
            "When the checks go red, the session reads the log, pushes a fix, and goes green again.",
            "",
            "## merge-train",
            "Pull requests queue, rebase onto main, and land in order.",
            "",
            "## close",
            "That was the week in the {Qontinui|Continuee} factory.",
            "",
        ]
    )


def draft_posts(
    week: Week, facts: Sequence[WeekFact], mode: Mode, record_url: str | None
) -> dict[str, str]:
    """One draft per network, within its limit. Facts are dropped (never
    altered) to fit; the illustrative note and the record link are kept."""
    known = [f for f in facts if f.known]
    note = (
        "The animation is illustrative; the numbers are from the build record."
        if mode == "illustrative"
        else "The animation replays the factory's build record."
    )
    head = f"Qontinui factory pulse, {week.describe()}."
    tail = f"Build record: {record_url}" if record_url else ""
    drafts: dict[str, str] = {}
    for network in NETWORKS:
        text = ""
        for n in range(len(known), -1, -1):
            body = "; ".join(f.phrase() for f in known[:n])
            parts = [head, (body[:1].upper() + body[1:] + ".") if body else "", note, tail]
            text = "\n\n".join(p for p in parts if p)
            if len(text) <= network.limit:
                break
        drafts[network.name] = text[: network.limit]
    return drafts


# ---- running studio-scenes -----------------------------------------------------


class SceneRunner(Protocol):
    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


def default_scenes_dir() -> Path:
    """The scenes package beside this Python package (the repo layout)."""
    return Path(__file__).resolve().parents[2] / "scenes"


def node_runner(scenes_dir: Path, timeout_s: float = 3600) -> SceneRunner:
    bin_path = scenes_dir / "bin" / "studio-scenes.mjs"

    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        node = shutil.which("node")
        if node is None:
            return subprocess.CompletedProcess(list(args), 127, "", "node is not on PATH")
        return subprocess.run(
            [node, str(bin_path), *args],
            cwd=scenes_dir,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )

    return run


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:400] if lines else ""


# ---- the run -------------------------------------------------------------------


@dataclass
class PulseResult:
    week: str
    outcome: Outcome
    mode: Mode | None = None
    reasons: list[str] = field(default_factory=list)
    record: dict[str, Any] | None = None
    facts: list[dict[str, Any]] = field(default_factory=list)
    narration: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    files: dict[str, str] = field(default_factory=dict)
    produced_at: str = ""
    pulse_version: int = PULSE_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"


PULSE_FILES = (
    "pulse.json",
    "review.html",
    "record.json",
    "script.md",
    "pulse.mp4",
    "pulse.mp4.composition.json",
    "pulse.mp4.vtt",
    "pulse.mp4.gate.json",
)


def pulse_dir(root: Path, week: Week) -> Path:
    return root / ".studio" / "pulse" / week.label


def _fetch(
    config: PulseConfig, client: BuildRecordClient
) -> tuple[LaunchProduct | None, FetchedRecord | None, str | None]:
    if config.product_path is None:
        return None, None, "no product is configured (pulse.yaml names none)"
    product = load_product(config.product_path)
    try:
        outcome = client.fetch(product.slug, product.record.door)
    except BuildRecordInvalid as e:
        return product, None, f"the build record was refused: {e}"
    if isinstance(outcome, NoBuildRecord):
        return product, None, outcome.describe()
    return product, outcome, None


#: Outcomes that settle a week: a later default run leaves them alone.
#: ``no-export`` and ``failed`` are retried by the next run (a record outage
#: or a broken toolchain on the scheduled day must not cost the week).
FINAL_OUTCOMES = ("ready", "blocked")


def run_pulse(
    root: Path,
    *,
    now: datetime,
    week_label: str | None = None,
    force: bool = False,
    config: PulseConfig | None = None,
    client: BuildRecordClient | None = None,
    runner: SceneRunner | None = None,
) -> PulseResult:
    """Produce one week's pulse. Never posts anything (operator-owned). Any
    unexpected error is recorded as a ``failed`` pulse, never left as an
    earlier run's outcome."""
    config = config or load_pulse_config(root)
    week = week_of(now, week_label)
    out = pulse_dir(root, week)
    existing = out / "pulse.json"
    if existing.exists() and not force:
        try:
            previous = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}  # unreadable: not a settled week, so produce it again
        if isinstance(previous, dict) and previous.get("outcome") in FINAL_OUTCOMES:
            return PulseResult(
                week=week.label,
                outcome=previous["outcome"],
                mode=previous.get("mode"),
                reasons=["already produced for this week (pass --force to redo it)"],
                produced_at=previous.get("produced_at", ""),
            )
    _prepare(out)
    result = PulseResult(week=week.label, outcome="failed", produced_at=now.isoformat())
    try:
        _produce(
            out,
            week,
            result,
            config,
            client or BuildRecordClient(),
            runner or node_runner(default_scenes_dir()),
        )
    except Exception as e:  # noqa: BLE001 - recorded, then the run ends as failed
        result.outcome = "failed"
        result.reasons.append(f"the pulse could not be produced: {type(e).__name__}: {e}")
        # A failed week holds no publish files or drafts, even half-written ones.
        _clear(out, ("publish", "drafts"))
        result.files = {}
    return _finish(out, result, week)


def _produce(
    out: Path,
    week: Week,
    result: PulseResult,
    config: PulseConfig,
    client: BuildRecordClient,
    runner: SceneRunner,
) -> None:
    product, fetched, why = _fetch(config, client)
    if fetched is None or product is None:
        result.outcome = "no-export"
        result.reasons.append(why or "no build record")
        return
    record_file = out / "record.json"
    record_file.write_text(json.dumps(fetched.document, indent=2) + "\n", encoding="utf-8")
    result.record = {
        "slug": product.slug,
        "url": fetched.url,
        "door": fetched.source,
        "sha256": fetched.content_sha256,
        "version": fetched.version,
        "generated_at": fetched.record.generated_at,
        # Only a frozen public snapshot may be linked from a post (as in the launch kit).
        "public_url": product.record_url() if fetched.publishable else None,
    }
    facts = week_facts(fetched.record, week)
    result.facts = [asdict(f) for f in facts]

    video = out / "pulse.mp4"
    common = ["--format", "mp4", "--aspect", config.aspect, "--out", str(video), "--force"]
    script = out / "script.md"
    narration_dir = out / "narration"

    def compose(mode: Mode, narrated: bool) -> subprocess.CompletedProcess[str]:
        status_line = (
            "Illustrative scenes; this week's numbers are from the build record."
            if mode == "illustrative"
            else "A replay of the factory's build record."
        )
        args = ["compose", COMPOSITION, *common]
        args += ["--var", f"week={week.describe()}", "--var", f"status_line={status_line}"]
        if mode == "replay":
            args += ["--build-record", str(record_file)]
        if narrated:
            args += ["--narration", str(narration_dir / f"{COMPOSITION}.timing.json")]
        return runner(args)

    # Probe for a replay. While replays are unavailable compose refuses (exit
    # 5) before rendering anything; once they exist the probe is a full
    # silent render, and the narrated render below replaces it.
    mode: Mode = "replay"
    probe = compose("replay", narrated=False)
    if probe.returncode == EXIT_REPLAY_UNAVAILABLE:
        result.reasons.append(
            f"replay unavailable, rendered illustrative: {_last_line(probe.stderr)}"
        )
        mode = "illustrative"
    elif probe.returncode != 0:
        result.reasons.append(
            f"compose failed (exit {probe.returncode}): {_last_line(probe.stderr)}"
        )
        return
    result.mode = mode

    script.write_text(narration_script(week, facts, mode), encoding="utf-8")
    voiced = runner(
        ["narrate", COMPOSITION, "--script", str(script), "--out", str(narration_dir), "--force"]
    )
    if voiced.returncode == 0:
        result.narration = {"status": "voiced", "detail": _last_line(voiced.stdout)}
    elif voiced.returncode == EXIT_NARRATION_UNAVAILABLE:
        reason = _last_line(voiced.stderr) or "no TTS service"
        result.narration = {"status": "unavailable", "detail": reason}
        result.reasons.append(f"no narration: {reason}")
    else:
        result.reasons.append(
            f"narrate failed (exit {voiced.returncode}): {_last_line(voiced.stderr)}"
        )
        return

    rendered = compose(mode, narrated=voiced.returncode == 0)
    if rendered.returncode != 0:
        result.reasons.append(
            f"compose failed (exit {rendered.returncode}): {_last_line(rendered.stderr)}"
        )
        return

    report = video.with_name(video.name + ".gate.json")
    gated = runner(["gate", str(video)])
    if gated.returncode not in (0, EXIT_GATE_BLOCKED) or not report.exists():
        result.reasons.append(
            f"the gate did not run (exit {gated.returncode}): {_last_line(gated.stderr)}"
        )
        return
    result.gate = json.loads(report.read_text(encoding="utf-8"))
    passed = gated.returncode == 0 and result.gate.get("verdict") == "pass"
    if passed and not fetched.publishable:
        result.outcome = "blocked"
        result.reasons.append(
            f"the record came from the {fetched.source} door, not a published public snapshot: "
            "readers could not check its numbers, so nothing is drafted"
        )
    elif passed:
        result.outcome = "ready"
        _publish(out, video, week, facts, mode, result)
    else:
        result.outcome = "blocked"
        failed = [c for c in result.gate.get("checks", []) if c.get("status") == "fail"]
        result.reasons += [f"gate {c['name']}: {c['detail']}" for c in failed] or [
            "the gate blocked it"
        ]


def _prepare(out: Path) -> None:
    """The pulse directory holds only this module's files: anything else in it
    is refused rather than overwritten or mixed into a publication. A redo
    first removes the previous run's outputs (its own files only, one by one),
    so no stale clip, captions, gate report or outcome survives into it."""
    out.mkdir(parents=True, exist_ok=True)
    allowed = set(PULSE_FILES) | {"narration", "publish", "drafts"}
    foreign = sorted(p.name for p in out.iterdir() if p.name not in allowed)
    if foreign:
        raise ConfigError(f"{out} holds files the pulse did not write: {', '.join(foreign[:5])}")
    for name in PULSE_FILES:
        f = out / name
        if f.is_symlink() or (f.exists() and not f.is_file()):
            raise ConfigError(f"{f} is not a plain file the pulse wrote; refusing to use it")
    _clear(out, ("narration", "publish", "drafts"))
    for name in PULSE_FILES:
        f = out / name
        if f.is_file():
            f.unlink()


def _clear(out: Path, subdirs: Sequence[str]) -> None:
    """Unlink the plain files in the pulse's own subdirectories (never a
    symlink, never recursively); a subdirectory that is a symlink or not a
    directory is refused."""
    for sub in subdirs:
        d = out / sub
        if d.is_symlink() or (d.exists() and not d.is_dir()):
            raise ConfigError(f"{d} is not a plain directory the pulse created; refusing to use it")
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and not f.is_symlink():
                    f.unlink()


def _publish(
    out: Path, video: Path, week: Week, facts: Sequence[WeekFact], mode: Mode, result: PulseResult
) -> None:
    publish = out / "publish"
    drafts = out / "drafts"
    publish.mkdir(exist_ok=True)
    drafts.mkdir(exist_ok=True)
    for name in (
        video.name,
        f"{video.name}.vtt",
        f"{video.name}.composition.json",
        f"{video.name}.gate.json",
    ):
        src = out / name
        if src.exists():
            shutil.copyfile(src, publish / name)
            result.files[f"publish/{name}"] = str(publish / name)
    record_url = (result.record or {}).get("public_url")
    for network, text in draft_posts(week, facts, mode, record_url).items():
        path = drafts / f"{network}.txt"
        path.write_text(text + "\n", encoding="utf-8")
        result.files[f"drafts/{network}.txt"] = str(path)


def _finish(out: Path, result: PulseResult, week: Week) -> PulseResult:
    result.files["review.html"] = str(out / "review.html")
    (out / "review.html").write_text(review_page(result, week, out), encoding="utf-8")
    (out / "pulse.json").write_text(result.to_json(), encoding="utf-8")
    return result


# ---- the review page -----------------------------------------------------------

_CSS = """
:root { --bg:#fafafa; --fg:#1d1d1f; --muted:#6b6b70; --card:#fff; --line:#e3e3e6; --ok:#1a7f37; --bad:#c62828; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --fg:#ececef; --muted:#9a9aa2; --card:#1d1d21; --line:#2c2c31; --ok:#4cc26a; --bad:#ff6b6b; } }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,sans-serif; max-width:960px; }
h1 { font-size:20px; } h2 { font-size:16px; margin:16px 0 6px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; margin:0 0 12px; }
.pass { color:var(--ok); } .fail { color:var(--bad); } .muted { color:var(--muted); }
video { width:100%; height:auto; border-radius:6px; }
pre { white-space:pre-wrap; background:var(--bg); border:1px solid var(--line); padding:8px; border-radius:4px; }
td, th { text-align:left; vertical-align:top; padding:2px 8px 2px 0; }
"""


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def review_page(result: PulseResult, week: Week, out: Path) -> str:
    """A static page for the operator: what the pulse produced, why it is (or
    is not) ready, and the drafts. It is not an approval queue: the gate
    already decided. Posting and the final publish click are the operator's."""
    verdicts = {
        "ready": ("pass", "Ready: the gate passed. Nothing has been posted."),
        "blocked": (
            "fail",
            "Blocked: the publish gate refused this clip. Nothing will be published."
            if not result.gate or result.gate.get("verdict") != "pass"
            else "Blocked: the gate passed, but the build record is not a published public "
            "snapshot, so its numbers are not posted. Nothing will be published.",
        ),
        "no-export": ("muted", "No export: there was no build record to make a pulse from."),
        "failed": ("fail", "Failed: the pulse could not be produced."),
    }
    cls, headline = verdicts[result.outcome]
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>Factory pulse {_e(week.label)}</title><style>{_CSS}</style></head><body>",
        f"<h1>Factory pulse — {_e(week.describe())}</h1>",
        f"<div class='card'><strong class='{cls}'>{_e(headline)}</strong>",
    ]
    if result.mode:
        mode_text = (
            "ILLUSTRATIVE: the scenes are synthetic traces; only the spoken and posted numbers come from the build record."
            if result.mode == "illustrative"
            else "REPLAY: the scenes replay the factory's build record."
        )
        parts.append(f"<p>{_e(mode_text)}</p>")
    if result.reasons:
        parts.append("<ul>" + "".join(f"<li>{_e(r)}</li>" for r in result.reasons) + "</ul>")
    parts.append("</div>")
    if result.record:
        parts.append(
            "<div class='card'><h2>Build record</h2>"
            f"<p>{_e(result.record.get('slug'))} via the {_e(result.record.get('door'))} door, "
            f"sha256 <code>{_e(result.record.get('sha256'))}</code></p><table>"
            + "".join(
                f"<tr><td>{_e(f['label'])}</td><td>"
                + (
                    _e(("≥ " if f["lower_bound"] else "") + str(f["value"]))
                    if f["value"] is not None
                    else f"unknown — {_e(f['unknown_reason'])}"
                )
                + "</td></tr>"
                for f in result.facts
            )
            + "</table></div>"
        )
    video = out / "pulse.mp4"
    if video.exists() and result.outcome in ("ready", "blocked"):
        track = (
            "<track kind='captions' src='pulse.mp4.vtt' srclang='en' default>"
            if (out / "pulse.mp4.vtt").exists()
            else ""
        )
        parts.append(
            f"<div class='card'><h2>Clip</h2><video controls preload='metadata' src='pulse.mp4'>{track}</video></div>"
        )
    if result.gate:
        rows = "".join(
            f"<tr><td class='{_e(c.get('status'))}'>{_e(str(c.get('status')).upper())}</td>"
            f"<td>{_e(c.get('name'))}</td><td>{_e(c.get('detail'))}"
            + "".join(
                f"<br><span class='muted'>warning: {_e(w)}</span>" for w in c.get("warnings", [])
            )
            + "</td></tr>"
            for c in result.gate.get("checks", [])
        )
        parts.append(f"<div class='card'><h2>Publish gate</h2><table>{rows}</table></div>")
    if result.narration:
        parts.append(
            f"<div class='card'><h2>Narration</h2><p>{_e(result.narration.get('status'))}: "
            f"{_e(result.narration.get('detail'))}</p></div>"
        )
    drafts = sorted((out / "drafts").glob("*.txt")) if (out / "drafts").exists() else []
    if drafts:
        parts.append("<div class='card'><h2>Draft posts</h2>")
        for d in drafts:
            parts.append(f"<h3>{_e(d.stem)}</h3><pre>{_e(d.read_text(encoding='utf-8'))}</pre>")
        parts.append("</div>")
    parts.append(
        "<p class='muted'>Operator-owned: which social accounts post these, and the final "
        "publish click. The pulse posts nothing.</p></body></html>"
    )
    return "\n".join(parts)


# ---- status --------------------------------------------------------------------


def pulse_history(root: Path) -> list[dict[str, Any]]:
    base = root / ".studio" / "pulse"
    rows: list[dict[str, Any]] = []
    if not base.exists():
        return rows
    for d in sorted(base.iterdir()):
        f = d / "pulse.json"
        if d.is_dir() and _WEEK.match(d.name) and f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            rows.append({"week": d.name, "outcome": data.get("outcome"), "mode": data.get("mode")})
    return rows


def ready_streak(rows: Sequence[dict[str, Any]]) -> int:
    """Consecutive ISO weeks, ending at the latest pulse, whose outcome is
    ``ready`` (the Phase 5 counter: four in a row with no authoring work)."""
    streak = 0
    expected: date | None = None
    for row in reversed(rows):
        m = _WEEK.match(row["week"])
        if not m:
            break
        monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        if expected is not None and monday != expected:
            break
        if row["outcome"] != "ready":
            break
        streak += 1
        expected = monday - timedelta(days=7)
    return streak


Clock = Callable[[], datetime]
