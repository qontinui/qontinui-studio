"""The weekly factory pulse (explainers plan Phase 5): the week, its numbers,
the narration script and drafts, and every outcome of a run, with a fake
studio-scenes runner and an in-process build-record door. No network, no
render: the scenes side is tested in scenes/."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from qontinui_studio.cli import main
from qontinui_studio.errors import ConfigError
from qontinui_studio.launch.posts import NETWORKS
from qontinui_studio.launch.record import parse_document
from qontinui_studio.pulse import (
    SEGMENTS,
    PulseConfig,
    WeekFact,
    draft_posts,
    narration_script,
    pulse_history,
    ready_streak,
    run_pulse,
    week_facts,
    week_of,
)

from .launch_support import document, public_client, write_product

NOW = datetime(2026, 10, 14, 6, 30, tzinfo=UTC)  # a Wednesday in ISO week 42


def test_week_is_the_previous_iso_week_by_default() -> None:
    week = week_of(NOW)
    assert (week.label, week.start.isoformat(), week.end.isoformat()) == (
        "2026-W41",
        "2026-10-05",
        "2026-10-11",
    )
    assert week_of(NOW, "2026-W01").start.isoformat() == "2025-12-29"
    with pytest.raises(ConfigError):
        week_of(NOW, "2026-41")
    with pytest.raises(ConfigError):
        week_of(NOW, "2026-W60")


def fresh() -> dict[str, Any]:
    """The fixture record, generated after week 41 ended."""
    doc = document()
    doc["generated_at"] = "2026-10-13T00:00:00Z"
    return doc


def test_week_facts_count_inside_the_week_and_never_invent_a_zero() -> None:
    record = parse_document(fresh())
    facts = {f.key: f for f in week_facts(record, week_of(NOW))}
    assert facts["prs_landed"].value == 2 and not facts["prs_landed"].lower_bound
    assert facts["work_units_shipped"].value == 1
    assert facts["work_units_shipped"].phrase() == "1 plan shipped"
    earlier = {f.key: f for f in week_facts(record, week_of(NOW, "2026-W40"))}
    assert earlier["prs_landed"].value == 0 and not earlier["prs_landed"].lower_bound
    doc = fresh()
    doc["unknowns"].append("prs: door_unavailable")
    unknown = {f.key: f for f in week_facts(parse_document(doc), week_of(NOW))}
    assert unknown["prs_landed"].value is None
    assert "door_unavailable" in (unknown["prs_landed"].unknown_reason or "")


def test_a_snapshot_older_than_the_week_end_gives_only_lower_bounds() -> None:
    facts = week_facts(parse_document(document()), week_of(NOW))  # generated 2026-10-09, mid-week
    assert all(f.lower_bound for f in facts)
    assert facts[0].phrase() == "at least 2 pull requests landed"


def test_an_item_whose_time_is_unknown_makes_the_count_a_lower_bound() -> None:
    doc = fresh()
    doc["prs"][1]["landed_at"] = None
    doc["unknowns"].append("prs[1].landed_at: not_established")
    facts = {f.key: f for f in week_facts(parse_document(doc), week_of(NOW))}
    assert (facts["prs_landed"].value, facts["prs_landed"].lower_bound) == (1, True)
    doc = fresh()
    doc["work_units"][0]["shipped_at"] = None  # status says shipped, time missing
    facts = {f.key: f for f in week_facts(parse_document(doc), week_of(NOW))}
    assert (facts["work_units_shipped"].value, facts["work_units_shipped"].lower_bound) == (0, True)
    doc = fresh()
    doc["prs"][1]["landed_at"] = None  # not landed, and the record does not doubt it
    facts = {f.key: f for f in week_facts(parse_document(doc), week_of(NOW))}
    assert (facts["prs_landed"].value, facts["prs_landed"].lower_bound) == (1, False)


def test_a_week_that_has_not_ended_is_refused_and_year_boundaries_hold() -> None:
    with pytest.raises(ConfigError, match="has not ended"):
        week_of(NOW, "2026-W42")
    jan = datetime(2027, 1, 6, tzinfo=UTC)
    assert week_of(jan).label == "2026-W53"


def test_narration_script_has_every_segment_and_says_it_is_illustrative() -> None:
    week = week_of(NOW)
    facts = [WeekFact("prs_landed", "pull requests landed", 3, singular="pull request landed")]
    script = narration_script(week, facts, "illustrative")
    assert [line[3:] for line in script.splitlines() if line.startswith("## ")] == list(SEGMENTS)
    assert "3 pull requests landed." in script
    assert "The scenes you see are illustrative" in script
    assert "October 5 to October 11" in script
    bare = narration_script(
        week, [WeekFact("prs_landed", "pull requests landed", None)], "illustrative"
    )
    assert "could not establish" in bare


def test_drafts_fit_every_network_and_keep_the_note_and_the_link() -> None:
    facts = [
        WeekFact("prs_landed", "pull requests landed", 12),
        WeekFact("work_units_shipped", "plans shipped", 3, lower_bound=True),
    ]
    url = "https://build.example.test/qontinui-studio"
    drafts = draft_posts(week_of(NOW), facts, "illustrative", url)
    assert set(drafts) == {n.name for n in NETWORKS}
    for network in NETWORKS:
        text = drafts[network.name]
        assert len(text) <= network.limit
        assert "illustrative" in text
        assert url in text
    assert "at least 3 plans shipped" in drafts["linkedin"]


# ---- runs ----------------------------------------------------------------------


class FakeScenes:
    """Answers like studio-scenes: compose/narrate/gate, writing the files the
    real commands write. ``replay``/``narration``/``gate`` pick the outcome."""

    def __init__(
        self,
        *,
        narration: bool = True,
        gate: bool = True,
        replay: bool = False,
        gate_crash: bool = False,
        narrate_crash: bool = False,
    ) -> None:
        self.narration, self.gate, self.replay = narration, gate, replay
        self.gate_crash, self.narrate_crash = gate_crash, narrate_crash
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        a = list(args)
        self.calls.append(a)
        done = lambda code, out="", err="": subprocess.CompletedProcess(a, code, out, err)  # noqa: E731
        if a[0] == "narrate":
            if self.narrate_crash:
                return done(1, err="TypeError: boom")
            if not self.narration:
                return done(6, err="narrate: no TTS service configured (set STUDIO_TTS_URL ...)")
            out = Path(a[a.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "factory-pulse.timing.json").write_text("{}")
            return done(0, out="voiced")
        if a[0] == "compose":
            if "--build-record" in a and not self.replay:
                return done(
                    5,
                    err="compose: --build-record: not implemented: needs the sibling plan's build-record export",
                )
            video = Path(a[a.index("--out") + 1])
            video.write_bytes(b"mp4")
            Path(f"{video}.composition.json").write_text("{}")
            if "--narration" in a:
                Path(f"{video}.vtt").write_text("WEBVTT\n")
            return done(0)
        if a[0] == "gate":
            if self.gate_crash:
                return done(2, err="gate: ffprobe is not installed")
            video = Path(a[1])
            checks = [
                {
                    "name": "speech",
                    "status": "pass" if self.gate else "fail",
                    "detail": "speech match 0.6 < 0.9",
                },
                {"name": "frames", "status": "pass", "detail": "ok"},
            ]
            verdict = "pass" if self.gate else "fail"
            Path(f"{video}.gate.json").write_text(
                json.dumps({"verdict": verdict, "checks": checks})
            )
            return done(0 if self.gate else 3)
        raise AssertionError(f"unexpected call {a}")


def fresh_client() -> Any:
    return public_client(fresh())


def _root(tmp_path: Path) -> tuple[Path, PulseConfig]:
    root = tmp_path / "studio"
    root.mkdir()
    product = write_product(root, capture=False, preview=False)
    (root / "pulse.yaml").write_text(f"product: {product.relative_to(root)}\n")
    from qontinui_studio.pulse import load_pulse_config

    return root, load_pulse_config(root)


def test_no_product_is_a_no_export_pulse_with_nothing_rendered(tmp_path: Path) -> None:
    scenes = FakeScenes()
    result = run_pulse(tmp_path, now=NOW, runner=scenes)
    assert result.outcome == "no-export"
    assert scenes.calls == []
    out = tmp_path / ".studio" / "pulse" / "2026-W41"
    assert json.loads((out / "pulse.json").read_text())["outcome"] == "no-export"
    page = (out / "review.html").read_text()
    assert "No export" in page and "posts nothing" in page


def test_no_record_is_a_no_export_pulse(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    result = run_pulse(
        root, now=NOW, config=config, client=public_client(public_status=404), runner=FakeScenes()
    )
    assert result.outcome == "no-export"
    assert "not_found" in result.reasons[0]


def test_replay_unavailable_renders_illustrative_and_a_failed_gate_blocks(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    scenes = FakeScenes(narration=False, gate=False)
    result = run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=scenes)
    assert result.outcome == "blocked"
    assert result.mode == "illustrative"
    assert any(r.startswith("replay unavailable") for r in result.reasons)
    assert any(r.startswith("no narration") for r in result.reasons)
    assert any(r.startswith("gate speech") for r in result.reasons)
    out = root / ".studio" / "pulse" / "2026-W41"
    assert not (out / "drafts").exists() or not any((out / "drafts").iterdir())
    assert not (out / "publish").exists() or not any((out / "publish").iterdir())
    # The probe tried the replay first, and the illustrative render says so on its card.
    compose = [c for c in scenes.calls if c[0] == "compose"]
    assert "--build-record" in compose[0] and "--build-record" not in compose[1]
    assert any(arg.startswith("status_line=Illustrative") for arg in compose[1])
    page = (out / "review.html").read_text()
    assert "Blocked" in page and "ILLUSTRATIVE" in page


def test_a_passing_gate_makes_the_pulse_ready_with_publish_files_and_drafts(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    scenes = FakeScenes()
    result = run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=scenes)
    assert result.outcome == "ready"
    out = root / ".studio" / "pulse" / "2026-W41"
    assert sorted(p.name for p in (out / "publish").iterdir()) == [
        "pulse.mp4",
        "pulse.mp4.composition.json",
        "pulse.mp4.gate.json",
        "pulse.mp4.vtt",
    ]
    drafts = {p.stem: p.read_text() for p in (out / "drafts").iterdir()}
    assert set(drafts) == {n.name for n in NETWORKS}
    assert all("illustrative" in text for text in drafts.values())
    assert all("build.example.test" in text for text in drafts.values())
    assert "2 pull requests landed" in drafts["linkedin"]
    narrated = [c for c in scenes.calls if c[0] == "compose" and "--narration" in c]
    assert narrated, "the final render carries the narration"
    assert "Draft posts" in (out / "review.html").read_text()


def test_a_week_is_produced_once_unless_forced(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=FakeScenes())
    again = FakeScenes()
    result = run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=again)
    assert again.calls == []
    assert result.outcome == "ready" and "already produced" in result.reasons[0]
    out = root / ".studio" / "pulse" / "2026-W41"
    assert (out / "pulse.mp4.vtt").exists()
    forced = FakeScenes(gate=False, narration=False)
    redo = run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=forced, force=True)
    assert redo.outcome == "blocked"
    assert not any((out / "drafts").iterdir()), "a blocked redo leaves no stale drafts"
    assert not any((out / "publish").iterdir())
    assert not (out / "pulse.mp4.vtt").exists(), "a silent redo keeps no stale captions"
    assert "<track" not in (out / "review.html").read_text()


def test_a_redo_that_crashes_records_failed_never_the_earlier_ready(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=FakeScenes())

    def explode(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(args), 3600)

    result = run_pulse(
        root, now=NOW, config=config, client=fresh_client(), runner=explode, force=True
    )
    assert result.outcome == "failed" and "TimeoutExpired" in result.reasons[-1]
    assert pulse_history(root)[0]["outcome"] == "failed"


def test_no_export_and_failed_weeks_are_retried_by_the_next_run(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    missing = public_client(public_status=404)
    assert (
        run_pulse(root, now=NOW, config=config, client=missing, runner=FakeScenes()).outcome
        == "no-export"
    )
    assert (
        run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=FakeScenes()).outcome
        == "ready"
    )


def test_a_gate_or_narrate_crash_is_failed_not_blocked(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    crashed = run_pulse(
        root, now=NOW, config=config, client=fresh_client(), runner=FakeScenes(gate_crash=True)
    )
    assert crashed.outcome == "failed" and "the gate did not run" in crashed.reasons[-1]
    crashed = run_pulse(
        root, now=NOW, config=config, client=fresh_client(), runner=FakeScenes(narrate_crash=True)
    )
    assert crashed.outcome == "failed" and "narrate failed" in crashed.reasons[-1]


def test_a_record_readers_cannot_check_never_reaches_ready(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    from qontinui_studio.launch.record import FetchedRecord

    class AuthedOnly:
        def fetch(self, slug: str, source: str) -> FetchedRecord:
            fetched = public_client(fresh()).fetch(slug, "public")
            assert isinstance(fetched, FetchedRecord)
            return FetchedRecord(
                fetched.record,
                fetched.document,
                "authed",
                "https://api.example.test/x",
                fetched.content_sha256,
            )

    result = run_pulse(root, now=NOW, config=config, client=AuthedOnly(), runner=FakeScenes())  # type: ignore[arg-type]
    assert result.outcome == "blocked"
    assert "not a published public snapshot" in result.reasons[-1]
    out = root / ".studio" / "pulse" / "2026-W41"
    assert not (out / "drafts").exists() or not any((out / "drafts").iterdir())


def test_when_replays_exist_the_pulse_is_a_replay(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    scenes = FakeScenes(replay=True)
    result = run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=scenes)
    assert (result.outcome, result.mode) == ("ready", "replay")
    composes = [c for c in scenes.calls if c[0] == "compose"]
    assert all("--build-record" in c for c in composes)
    assert "--narration" in composes[-1]
    drafts = (root / ".studio" / "pulse" / "2026-W41" / "drafts" / "linkedin.txt").read_text()
    assert "illustrative" not in drafts and "replays" in drafts


def test_a_foreign_file_in_the_pulse_directory_is_refused(tmp_path: Path) -> None:
    out = tmp_path / ".studio" / "pulse" / "2026-W41"
    out.mkdir(parents=True)
    (out / "notes.txt").write_text("mine")
    with pytest.raises(ConfigError, match="notes.txt"):
        run_pulse(tmp_path, now=NOW, runner=FakeScenes())
    assert (out / "notes.txt").read_text() == "mine"


def test_ready_streak_counts_consecutive_weeks_ending_at_the_latest() -> None:
    rows: list[dict[str, Any]] = [
        {"week": "2026-W38", "outcome": "ready"},
        {"week": "2026-W39", "outcome": "blocked"},
        {"week": "2026-W40", "outcome": "ready"},
        {"week": "2026-W41", "outcome": "ready"},
    ]
    assert ready_streak(rows) == 2
    assert ready_streak([*rows, {"week": "2026-W43", "outcome": "ready"}]) == 1  # W42 missing
    assert ready_streak([]) == 0


def test_cli_run_and_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "pulse", "run", "--week", "2026-W41"])
    assert result.exit_code == 0, result.output
    assert "pulse 2026-W41: no-export" in result.output
    status = runner.invoke(main, ["--root", str(tmp_path), "pulse", "status"])
    assert "2026-W41  no-export" in status.output
    assert "consecutive ready weeks: 0" in status.output
    assert pulse_history(tmp_path)[0]["outcome"] == "no-export"


def test_a_snapshot_from_before_the_week_knows_nothing_about_it() -> None:
    doc = document()
    doc["generated_at"] = "2026-09-01T00:00:00Z"
    facts = week_facts(parse_document(doc), week_of(NOW))
    assert all(
        f.value is None and "before the week began" in (f.unknown_reason or "") for f in facts
    )


def test_a_demoted_plan_is_not_counted_as_shipped() -> None:
    doc = fresh()
    doc["work_units"][0]["status"] = "in_progress"
    facts = {f.key: f for f in week_facts(parse_document(doc), week_of(NOW))}
    assert facts["work_units_shipped"].value == 0


def test_a_symlinked_pulse_file_is_refused_and_a_corrupt_one_is_retried(tmp_path: Path) -> None:
    out = tmp_path / ".studio" / "pulse" / "2026-W41"
    out.mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (out / "pulse.json").symlink_to(target)
    with pytest.raises(ConfigError, match="not a plain file"):
        run_pulse(tmp_path, now=NOW, runner=FakeScenes())
    assert target.read_text() == "{}"
    (out / "pulse.json").unlink()
    (out / "pulse.json").write_text("{not json")
    assert run_pulse(tmp_path, now=NOW, runner=FakeScenes()).outcome == "no-export"


def test_a_publish_that_fails_part_way_leaves_no_publish_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import qontinui_studio.pulse as pulse_module

    root, config = _root(tmp_path)
    real = pulse_module.draft_posts

    def broken(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise OSError("disk full")

    monkeypatch.setattr(pulse_module, "draft_posts", broken)
    result = run_pulse(root, now=NOW, config=config, client=fresh_client(), runner=FakeScenes())
    assert result.outcome == "failed" and "disk full" in result.reasons[-1]
    out = root / ".studio" / "pulse" / "2026-W41"
    assert not any((out / "publish").iterdir())
    monkeypatch.setattr(pulse_module, "draft_posts", real)


def test_the_non_public_cap_has_its_own_headline(tmp_path: Path) -> None:
    root, config = _root(tmp_path)
    from qontinui_studio.launch.record import FetchedRecord

    class AuthedOnly:
        def fetch(self, slug: str, source: str) -> FetchedRecord:
            fetched = public_client(fresh()).fetch(slug, "public")
            assert isinstance(fetched, FetchedRecord)
            return FetchedRecord(
                fetched.record,
                fetched.document,
                "authed",
                "https://api.example.test/x",
                fetched.content_sha256,
            )

    run_pulse(root, now=NOW, config=config, client=AuthedOnly(), runner=FakeScenes())  # type: ignore[arg-type]
    page = (root / ".studio" / "pulse" / "2026-W41" / "review.html").read_text()
    assert "the gate passed, but the build record is not a published public snapshot" in page
    assert "refused this clip" not in page
