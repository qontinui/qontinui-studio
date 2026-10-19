"""``studio compile`` (plan 2026-10-13 Phase 19) on a fixture slot set with the
fake provider: missing and stale slots generate, the pick is recorded as
unjudged, the derivatives and the manifest land in the checkout through the
existing publish path with the IPTC marker on every one, and the evidence is
written. The dial is honoured before any batch exists."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio import marker
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.compiler.evidence import render_markdown
from qontinui_studio.compiler.inputs import Digest
from qontinui_studio.compiler.run import PICK_UNJUDGED, AltText, lucide_selection_sha256
from qontinui_studio.compiler.specs import load_spec
from qontinui_studio.errors import ConfigError, ProviderDeferred
from qontinui_studio.imagegen_policy import DialReading, ImagegenPolicy
from qontinui_studio.locks import LOCK_NAME
from qontinui_studio.pipeline import Context, resume_all
from qontinui_studio.postprocess import sha256_file

from .compile_support import IMAGERY, IMAGERY_2, CompileEnv, make_checkout
from .conftest import FakeClock

HERO = "marketing.home.hero"
HERO_FILE = HERO  # the slot id is the file stem and the manifest key


@pytest.fixture
def env(
    tmp_path: Path,
    studio_root: Path,
    make_context: Callable[..., Context],
    monkeypatch: pytest.MonkeyPatch,
) -> CompileEnv:
    monkeypatch.delenv("STUDIO_IMAGEGEN_DIR", raising=False)
    e = CompileEnv(studio_root, make_checkout(tmp_path / "checkout"), make_context)
    e.write_target()
    return e


def batches(ctx: Context) -> list[str]:
    return [r["id"] for r in ctx.conn.execute("SELECT id FROM batches ORDER BY created_at")]


def submitted(ctx: Context) -> int:
    return sum(
        len(a.submitted) for a in ctx.adapters.adapters.values() if isinstance(a, FakeImageAdapter)
    )


# ---- the gate: a slot spec with no asset yields derivatives, manifest, evidence --


def test_a_missing_slot_compiles_into_the_checkout(env: CompileEnv) -> None:
    spec_path = env.write_spec(HERO)
    report = env.run()
    [slot] = report.slots
    assert slot.status == "compiled", slot.reasons
    assert report.exit_code() == 0 and report.files_written

    # Derivatives: AVIF + WebP + a JPEG fallback (a photographic role), every
    # one carrying the IPTC trainedAlgorithmicMedia marker.
    files = {p.name for p in env.publish_dir.iterdir()}
    assert {f"{HERO_FILE}.avif", f"{HERO_FILE}.webp", f"{HERO_FILE}.jpg"} <= files
    for ext in ("avif", "webp", "jpg"):
        assert marker.is_marked(env.publish_dir / f"{HERO_FILE}.{ext}"), ext
    # The checkout carries nothing else: no lock, journal or staging debris.
    assert files == {
        f"{HERO_FILE}.avif",
        f"{HERO_FILE}.webp",
        f"{HERO_FILE}.jpg",
        "studio-manifest.json",
    }
    assert not (env.publish_dir / LOCK_NAME).exists()

    # The manifest is the existing studio-manifest.json, extended with the
    # slot spec's hash and its binding.
    manifest = json.loads((env.publish_dir / "studio-manifest.json").read_text())
    entry = manifest["slots"][HERO_FILE]
    spec = load_spec(spec_path)
    assert entry["slot_id"] == HERO and entry["slot_spec_sha256"] == spec.sha256
    assert entry["ai_generated"] is True
    assert entry["digital_source_type"] == marker.DIGITAL_SOURCE_TYPE
    assert set(entry["provenance"]) == {"avif", "webp", "jpeg"}
    binding = entry["binding"]
    assert binding["spec_sha256"] == spec.sha256
    assert binding["source"] == "generated" and binding["ai_generated"] is True
    assert binding["imagery_sha256"] == IMAGERY
    assert len(binding["recipe_sha256"]) == 64
    # Content addressing: the binding names the picked candidate's ORIGINAL
    # bytes — the hash the image store keys it by.
    assert entry["pick"]["pick"] == PICK_UNJUDGED and entry["pick"]["index"] == 0
    raw_candidates = [u for u in env.store.uploads if u["kind"] == "candidate"]
    assert len(raw_candidates) == 2  # N = 2: every candidate went up
    assert binding["asset_sha256"] == raw_candidates[0]["sha256"]
    for fmt, name in entry["files"].items():
        assert entry["sha256"][fmt] == sha256_file(env.publish_dir / name)

    # The store: candidates proposed, the derivatives uploaded, the pick chosen.
    derivs = [u for u in env.store.uploads if u["kind"] == "derivative"]
    assert sorted(d["sha256"] for d in derivs) == sorted(entry["sha256"].values())
    statuses = [p["status"] for p in env.store.proposals]
    assert statuses == ["candidate", "chosen"]  # the other candidate, then the pick
    chosen = env.store.proposals[-1]
    assert chosen["repo"] == "qontinui/qontinui-web" and chosen["slot_id"] == HERO
    assert chosen["asset_sha256"] == binding["asset_sha256"]
    assert chosen["judge"]["pick"] == PICK_UNJUDGED

    # Evidence: values with provenance, and the two typed UNKNOWNs naming the
    # phases they wait on.
    body = render_markdown(report)
    assert PICK_UNJUDGED in body and binding["asset_sha256"] in body
    assert "Phase 18" in body and "Phase 15" in body
    assert "capability_unavailable_in_build" in body
    assert "160px dark @1x" in body and "320px dark @1x" in body


def test_a_fresh_slot_is_left_alone(env: CompileEnv) -> None:
    env.write_spec(HERO)
    env.run()
    ctx = env.context()
    before = (batches(ctx), submitted(ctx), len(env.store.uploads))
    again = env.run()
    assert [s.status for s in again.slots] == ["fresh"]
    assert (batches(ctx), submitted(ctx), len(env.store.uploads)) == before
    assert again.exit_code() == 0 and not again.files_written


def test_an_unmarked_derivative_is_never_written(
    env: CompileEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5 runs unchanged: a derivative without the marker refuses the slot,
    and nothing reaches the checkout."""
    real = marker.save_options

    def unmarked(fmt: str) -> dict[str, object]:
        return {} if fmt in ("avif", "webp") else real(fmt)

    monkeypatch.setattr("qontinui_studio.postprocess.marker.save_options", unmarked)
    env.write_spec(HERO)
    report = env.run()
    [slot] = report.slots
    assert slot.status == "refused"
    assert any("lacks the AI-generated marker" in r for r in slot.reasons)
    assert report.exit_code() == 2
    assert not any(p.suffix in (".avif", ".webp") for p in env.publish_dir.glob("*"))


# ---- staleness: spec, imagery, and slot matching -----------------------------------


def test_an_imagery_change_makes_the_binding_stale_for_changed(env: CompileEnv) -> None:
    env.write_spec(HERO)
    env.run()
    first = json.loads((env.publish_dir / "studio-manifest.json").read_text())["slots"][HERO_FILE]
    env.imagery["domain_spec/visual-system#imagery"] = Digest(IMAGERY_2, "v4#imagery")
    plan = env.run(dry_run=True, changed=True)
    assert [s.status for s in plan.slots] == ["planned"]
    assert any("imagery changed" in r for r in plan.slots[0].reasons)
    report = env.run(changed=True)
    assert [s.status for s in report.slots] == ["compiled"]
    second = json.loads((env.publish_dir / "studio-manifest.json").read_text())["slots"][HERO_FILE]
    assert second["binding"]["imagery_sha256"] == IMAGERY_2
    assert second["batch_id"] != first["batch_id"]
    # The overnight run is a BULK job: the host defers it outside its window.
    assert all(a.submitted[-1].priority == "bulk" for a in _fakes(env))


def _fakes(env: CompileEnv) -> list[FakeImageAdapter]:
    return [
        a
        for a in env.context().adapters.adapters.values()
        if isinstance(a, FakeImageAdapter) and a.submitted
    ]


def test_changed_leaves_a_missing_slot_and_compiles_only_stale_ones(env: CompileEnv) -> None:
    env.write_spec(HERO)
    report = env.run(changed=True)
    assert [s.status for s in report.slots] == ["missing"]
    assert batches(env.context()) == []


def test_slot_matching_a_binding_belongs_to_its_own_slot(env: CompileEnv) -> None:
    env.write_spec(HERO)
    env.write_spec("marketing.home.closing", job="Close the page on a quiet workshop scene.")
    report = env.run()
    assert [s.status for s in report.slots] == ["compiled", "compiled"]
    manifest = json.loads((env.publish_dir / "studio-manifest.json").read_text())["slots"]
    hero, closing = manifest[HERO_FILE], manifest["marketing.home.closing"]
    assert hero["slot_id"] == HERO and closing["slot_id"] == "marketing.home.closing"
    assert hero["binding"]["asset_sha256"] != closing["binding"]["asset_sha256"]
    assert hero["binding"]["spec_sha256"] != closing["binding"]["spec_sha256"]
    # Editing ONE spec makes exactly that slot stale.
    env.write_spec(HERO, job="Show a calm run finishing, seen from behind the operator.")
    plan = env.run(dry_run=True)
    states = {s.slot_id: (s.status, s.reasons) for s in plan.slots}
    assert states[HERO][0] == "planned" and "spec changed" in states[HERO][1]
    assert states["marketing.home.closing"][0] == "fresh"
    # Each slot was judged against ITS OWN binding, never a neighbour's.
    judged = {s.slot_id: s.binding for s in plan.slots}
    assert judged[HERO] == hero["binding"]
    assert judged["marketing.home.closing"] == closing["binding"]


# ---- the dial ------------------------------------------------------------------


def test_the_dial_off_refuses_before_any_batch_or_provider_call(env: CompileEnv) -> None:
    env.write_spec(HERO)
    ctx = env.context()
    seen: list[str | None] = []

    def off(repo: str | None) -> DialReading:
        seen.append(repo)
        return DialReading("off", "coord answered", "repo")

    ctx.imagegen = ImagegenPolicy(off)
    report = env.run()
    assert [s.status for s in report.slots] == ["generation_refused"]
    assert report.refusal is not None and report.refusal["code"] == "image_generation_off"
    assert report.exit_code() == 6
    assert seen == ["qontinui/qontinui-web"]  # read for the target's repo
    assert batches(ctx) == [] and submitted(ctx) == 0 and env.store.uploads == []
    assert not env.publish_dir.exists()


def test_the_machine_switch_refuses_too(env: CompileEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    env.write_spec(HERO)
    monkeypatch.setenv("QONTINUI_IMAGEGEN", "0")
    report = env.run()
    assert report.refusal is not None and report.refusal["code"] == "image_generation_off"
    assert batches(env.context()) == []


def test_a_dry_run_reads_no_dial_and_writes_nothing(env: CompileEnv) -> None:
    env.write_spec(HERO)
    ctx = env.context()
    ctx.imagegen = ImagegenPolicy(lambda repo: pytest.fail("a dry run read the dial"))
    report = env.run(dry_run=True)
    assert [s.status for s in report.slots] == ["planned"]
    assert batches(ctx) == [] and not env.publish_dir.exists()


# ---- UNKNOWN inputs ------------------------------------------------------------


def test_an_unreadable_imagery_treatment_generates_nothing(env: CompileEnv) -> None:
    env.write_spec(HERO)
    env.imagery["domain_spec/visual-system#imagery"] = Digest(None, "no usable device credential")
    report = env.run()
    [slot] = report.slots
    assert slot.status == "unknown" and report.exit_code() == 8
    assert any("imagery: UNKNOWN" in r for r in slot.reasons)
    assert batches(env.context()) == []


def test_unknown_imagery_after_binding_is_unknown_not_fresh(env: CompileEnv) -> None:
    env.write_spec(HERO)
    env.run()
    env.imagery["domain_spec/visual-system#imagery"] = Digest(None, "coord unreachable")
    report = env.run()
    assert [s.status for s in report.slots] == ["unknown"]
    assert report.exit_code() == 8


def test_a_lora_slot_without_a_licence_manifest_generates_nothing(env: CompileEnv) -> None:
    env.write_spec(HERO, lora="qontinui-house-v1")
    report = env.run()
    [slot] = report.slots
    assert slot.status == "unknown"
    assert any("lora: UNKNOWN" in r for r in slot.reasons)
    assert batches(env.context()) == []


# ---- routes ----------------------------------------------------------------------


def test_logo_and_screenshot_slots_are_never_compiled(env: CompileEnv) -> None:
    env.write_spec("brand.header-logo", "logo", aspect="1/1")
    env.write_spec("docs.runner.screenshot", "screenshot")
    report = env.run()
    assert {s.slot_id: s.status for s in report.slots} == {
        "brand.header-logo": "not_compiled",
        "docs.runner.screenshot": "not_compiled",
    }
    assert batches(env.context()) == []


def test_a_traced_slot_lands_as_a_marked_svg(env: CompileEnv) -> None:
    env.write_spec("sessions.empty-state", "empty_state", aspect="4/3")
    report = env.run()
    [slot] = report.slots
    assert slot.route == "traced" and slot.status == "compiled", slot.reasons
    svg = env.publish_dir / "sessions.empty-state.svg"
    assert marker.is_marked(svg)
    root = marker.svg_root(svg.read_bytes())
    # empty_state lays out at most 480 CSS px; the widest breakpoint (320) at DPR 1 caps it.
    assert root is not None and root.get("viewBox") == "0 0 320 240"
    assert (root.get("width"), root.get("height")) == ("320", "240")
    assert {p.name for p in env.publish_dir.iterdir()} == {
        "sessions.empty-state.svg",
        "studio-manifest.json",
    }


def test_an_og_card_is_one_png_checked_by_fetch(env: CompileEnv) -> None:
    env.write_spec("marketing.og", "og_card", aspect="1200/630")
    report = env.run()
    assert [s.status for s in report.slots] == ["compiled"]
    assert {p.name for p in env.publish_dir.iterdir()} == {
        "marketing.og.png",
        "studio-manifest.json",
    }
    gates = {(g["gate"], g["outcome"]) for g in report.slots[0].gates}
    assert ("d7_preview", "pass") in gates


def test_a_lucide_slot_names_its_component_and_writes_nothing(env: CompileEnv) -> None:
    env.write_spec("sessions.avatar", "avatar_fallback", aspect="1/1")
    env.write_target("slots:\n  sessions.avatar: {icon: circle-user}\n")
    report = env.run()
    [slot] = report.slots
    assert slot.status == "unverified" and report.exit_code() == 8  # no lucide-react installed
    icons = env.checkout / "frontend" / "node_modules" / "lucide-react" / "dist" / "esm" / "icons"
    icons.mkdir(parents=True)
    (icons.parent.parent.parent / "package.json").write_text('{"version": "0.562.0"}')
    (icons / "circle-user.js").write_text("export {}")
    report = env.run()
    [slot] = report.slots
    assert slot.status == "selected" and slot.lucide == {
        "icon": "circle-user",
        "component": "CircleUser",
        "detail": "lucide-react 0.562.0",
    }
    assert batches(env.context()) == []
    # The manifest-only entry the web <SlotImage> renders the component from:
    # no image file, a binding that addresses the selection.
    manifest = json.loads((env.publish_dir / "studio-manifest.json").read_text())
    entry = manifest["slots"]["sessions.avatar"]
    assert entry == {
        "route": "lucide",
        "slot_id": "sessions.avatar",
        "slot_spec_sha256": slot.spec_sha256,
        "lucide": {"package": "lucide-react", "icon": "circle-user", "component": "CircleUser"},
        "files": {},
        "sha256": {},
        "ai_generated": False,
        "binding": {
            "asset_sha256": lucide_selection_sha256("circle-user"),
            "spec_sha256": slot.spec_sha256,
            "source": "selected",
            "ai_generated": False,
        },
    }
    assert {p.name for p in env.publish_dir.iterdir()} == {"studio-manifest.json"}
    assert report.files_written and slot.manifest_written
    again = env.run()
    assert again.slots[0].status == "selected" and not again.files_written  # current: no rewrite
    env.write_target("slots:\n  sessions.avatar: {icon: no-such-icon}\n")
    assert [s.status for s in env.run().slots] == ["icon_unknown"]


SVG_OK = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" data-slot-spec-sha256="{h}">
<metadata>{x}</metadata>
<path d="M0 0 L10 10" stroke="currentColor" fill="none"/>
<circle cx="5" cy="5" r="2" fill="var(--qontinui-brand-primary)"/>
</svg>"""


def test_an_svg_slot_is_authored_by_the_session_and_validated(env: CompileEnv) -> None:
    from qontinui_studio.compiler.selected import _xmpmeta

    env.write_spec("onboarding.connect", "onboarding", aspect="4/3")
    env.write_target("slots:\n  onboarding.connect: {route: svg}\n")
    report = env.run()
    [slot] = report.slots
    assert slot.status == "awaiting_authoring" and report.exit_code() == 7
    assert slot.authoring is not None
    assert slot.authoring["root"]["data-slot-spec-sha256"] == slot.spec_sha256
    path = env.slots_dir / "onboarding.connect.svg"
    path.write_text(SVG_OK.format(h=slot.spec_sha256, x=_xmpmeta()))
    assert [s.status for s in env.run().slots] == ["authored"]
    # A literal colour, or a missing marker, is invalid; an old hash is stale.
    path.write_text(
        SVG_OK.format(h=slot.spec_sha256, x=_xmpmeta()).replace("currentColor", "#ff0000")
    )
    assert [s.status for s in env.run().slots] == ["authored_invalid"]
    path.write_text(SVG_OK.format(h=slot.spec_sha256, x=""))
    bad = env.run().slots[0]
    assert bad.status == "authored_invalid" and any("marker" in r for r in bad.reasons)
    path.write_text(SVG_OK.format(h="0" * 64, x=_xmpmeta()))
    assert [s.status for s in env.run().slots] == ["authored_stale"]
    assert not env.publish_dir.exists()  # studio writes no authored art


# ---- alt text, resumption, re-homing, ownership --------------------------------------


def test_an_alt_required_slot_waits_for_its_alt_text_then_publishes(env: CompileEnv) -> None:
    env.write_spec(HERO, alt="kind: required")
    first = env.run()
    [slot] = first.slots
    assert slot.status == "awaiting_alt_text" and first.exit_code() == 7
    assert slot.pick is not None and slot.derivatives
    calls = submitted(env.context())
    sha = slot.pick["asset_sha256"]
    # An alt text written for ANOTHER asset is not applied.
    wrong = env.run(alt_texts={HERO: AltText("f" * 64, "A person at a desk.")})
    assert wrong.slots[0].status == "awaiting_alt_text"
    done = env.run(alt_texts={HERO: AltText(sha, "A person at a desk watches a run finish.")})
    assert [s.status for s in done.slots] == ["compiled"]
    assert submitted(env.context()) == calls  # resumed, not regenerated
    entry = json.loads((env.publish_dir / "studio-manifest.json").read_text())["slots"][HERO_FILE]
    assert entry["binding"]["alt_text"] == "A person at a desk watches a run finish."


class DeferOnce(FakeImageAdapter):
    """Defers the first submit to tomorrow (a bulk job outside the window)."""

    def __init__(self) -> None:
        super().__init__("fake", "fake-v1")
        self.deferred = False

    def submit(self, request, staging_dir):  # type: ignore[no-untyped-def]
        if not self.deferred:
            self.deferred = True
            raise ProviderDeferred("deferred: outside the bulk window", 86400.0)
        return super().submit(request, staging_dir)


def test_a_deferred_slot_resumes_its_own_batch(env: CompileEnv) -> None:
    env.write_spec(HERO)
    ctx = env.context()
    ctx.adapters.adapters["fake"] = DeferOnce()
    first = env.run()
    [slot] = first.slots
    assert slot.status == "generating" and first.exit_code() == 3
    assert any("studio compile" in r for r in slot.reasons)
    assert not any("studio jobs run" in r for r in slot.reasons)
    # `jobs run` never drives a compile batch.
    assert resume_all(ctx) == []
    clock = ctx.clock
    assert isinstance(clock, FakeClock)
    clock.now += timedelta(days=1)
    second = env.run()
    assert [s.status for s in second.slots] == ["compiled"]
    assert second.slots[0].batch_id == slot.batch_id and len(batches(ctx)) == 1


def test_a_batch_published_into_another_checkout_is_rehomed_not_regenerated(
    env: CompileEnv, tmp_path: Path
) -> None:
    env.write_spec(HERO)
    first = env.run()
    calls = submitted(env.context())
    other = make_checkout(tmp_path / "checkout2")
    (other / "frontend" / "image-slots" / f"{HERO}.yaml").write_text(
        (env.slots_dir / f"{HERO}.yaml").read_text()
    )
    second = env.run(checkout=other)
    assert [s.status for s in second.slots] == ["compiled"]
    assert second.slots[0].batch_id == first.slots[0].batch_id
    assert submitted(env.context()) == calls
    assert (other / "frontend" / "public" / "image-slots" / f"{HERO_FILE}.avif").is_file()
    # The first checkout keeps its files: publishing into one never touches another.
    assert (env.publish_dir / f"{HERO_FILE}.avif").is_file()


def _snapshot(publish_dir: Path) -> dict[str, str]:
    return {p.name: sha256_file(p) for p in sorted(publish_dir.iterdir())}


def test_compiling_into_one_worktree_never_touches_another(env: CompileEnv, tmp_path: Path) -> None:
    """Two worktrees of one repository share the target's site id: a compile
    into B must not move, delete or supersede what A holds."""
    env.write_spec(HERO)
    assert [s.status for s in env.run().slots] == ["compiled"]
    before = _snapshot(env.publish_dir)
    other = make_checkout(tmp_path / "checkout2")
    (other / "frontend" / "image-slots" / f"{HERO}.yaml").write_text(
        (env.slots_dir / f"{HERO}.yaml").read_text().replace("calm automation", "quiet automation")
    )
    second = env.run(checkout=other)
    assert [s.status for s in second.slots] == ["compiled"]
    assert _snapshot(env.publish_dir) == before  # A untouched, byte for byte
    assert (other / "frontend" / "public" / "image-slots" / f"{HERO_FILE}.avif").is_file()
    live = {
        Path(r["published_path"]).parent
        for r in env.context().conn.execute(
            "SELECT published_path FROM publications WHERE unpublished_at IS NULL"
        )
    }
    # A's publications stay live (A still holds them), beside B's.
    assert live == {env.publish_dir, other / "frontend" / "public" / "image-slots"}
    # And A, compiled again, is still fresh.
    assert [s.status for s in env.run().slots] == ["fresh"]


def test_a_slot_another_studio_published_is_replaced_but_a_hand_placed_file_is_not(
    env: CompileEnv, studio_root: Path, make_context: Callable[..., Context]
) -> None:
    env.write_spec(HERO)
    env.run()
    # A fresh studio database (another machine) sees the checkout's files: the
    # checkout's own manifest vouches for them.
    shutil.rmtree(studio_root / ".studio")
    env.ctx = make_context()
    env.write_spec(HERO, job="Show the run finishing, the operator relaxed.")
    assert [s.status for s in env.run().slots] == ["compiled"]
    # A file the manifest does not record is someone's own: never overwritten.
    env.write_spec(HERO, job="A third version of the hero.")
    (env.publish_dir / f"{HERO_FILE}.webp").write_bytes(b"owner-supplied")
    report = env.run()
    [slot] = report.slots
    assert slot.status == "refused" and any("foreign_file" in r for r in slot.reasons)
    assert (env.publish_dir / f"{HERO_FILE}.webp").read_bytes() == b"owner-supplied"


# ---- configuration -----------------------------------------------------------------


def test_compile_writes_only_into_a_linked_worktree(env: CompileEnv, tmp_path: Path) -> None:
    env.write_spec(HERO)
    (env.checkout / ".git").unlink()
    with pytest.raises(ConfigError, match="not a linked git worktree"):
        env.run()
    # A primary checkout (its .git a directory) is shared: refused.
    (env.checkout / ".git").mkdir()
    with pytest.raises(ConfigError, match="primary checkout"):
        env.run()
    (env.checkout / ".git").rmdir()
    (env.checkout / ".git").write_text("gitdir: /somewhere/.git\n")  # not a worktree's git dir
    with pytest.raises(ConfigError, match="not a linked git worktree"):
        env.run()
    assert not env.publish_dir.exists()


def test_the_slots_path_must_end_with_the_targets_slots_dir(
    env: CompileEnv, tmp_path: Path
) -> None:
    from qontinui_studio.compiler.run import CompileOptions, compile_slots
    from qontinui_studio.compiler.target import load_target

    target = load_target(env.studio_root / "targets" / "web.yaml", env.studio_root)
    with pytest.raises(ConfigError, match="does not end with"):
        compile_slots(
            env.context(),
            CompileOptions(slots_dir=tmp_path, target=target),
            None,  # type: ignore[arg-type]
        )


def test_a_target_may_not_reuse_a_site_id(env: CompileEnv) -> None:
    env.write_spec(HERO)
    text = (
        (env.studio_root / "targets" / "web.yaml")
        .read_text()
        .replace("id: web", "id: cafe-template")
    )
    (env.studio_root / "targets" / "web.yaml").write_text(text)
    with pytest.raises(ConfigError, match="also a site id"):
        env.run()


# ---- the CLI -----------------------------------------------------------------------


def test_the_cli_writes_report_and_evidence_and_refuses_publish_of_a_compile_batch(
    env: CompileEnv, studio_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qontinui_studio import cli

    env.write_spec(HERO)
    real_context = cli._context

    def context(root: Path, generate: bool = False) -> Context:
        ctx = real_context(root, generate)
        made = env.make_context()
        ctx.bridge, ctx.fetch, ctx.clock, ctx.sleep = (
            made.bridge,
            made.fetch,
            made.clock,
            made.sleep,
        )
        ctx.adapters = made.adapters
        return ctx

    monkeypatch.setattr(cli, "_context", context)
    monkeypatch.setattr(cli, "HttpImageStore", lambda repo: env.store)
    monkeypatch.setattr(cli, "ImageryReader", lambda: env.imagery_reader)
    out = tmp_path / "evidence.md"
    result = CliRunner().invoke(
        cli.main,
        [
            "--root",
            str(studio_root),
            "compile",
            "--slots",
            str(env.slots_dir),
            "--json",
            "--evidence",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["files_written"] is True and data["slots"][0]["status"] == "compiled"
    assert data["slots"][0]["evidence"]["values"]["status"] == "unknown"
    state = studio_root / ".studio" / "compile" / "web"
    assert json.loads((state / "report.json").read_text())["exit_code"] == 0
    assert "Phase 18" in out.read_text()
    assert (state / "evidence.md").read_text() == out.read_text()
    # Evidence inside the checkout is refused: the repo carries nothing else.
    inside = CliRunner().invoke(
        cli.main,
        [
            "--root",
            str(studio_root),
            "compile",
            "--slots",
            str(env.slots_dir),
            "--evidence",
            str(env.checkout / "evidence.md"),
        ],
    )
    assert inside.exit_code != 0 and "inside the checkout" in inside.output
    batch = data["slots"][0]["batch_id"]
    for verb in ("publish", "unpublish"):
        r = CliRunner().invoke(cli.main, ["--root", str(studio_root), verb, "--batch", batch])
        assert r.exit_code == 2 and "compile target 'web'" in r.output


# ---- the fleet's own facade: engine recipe hashes and LoRA weights ---------------------

LOCAL_ROW = """
  - id: local-gpu
    provider: Local image facade
    plan: test
    api_available: {value: "yes", source: "https://facade.test/api"}
    automation_allowed: {value: "yes", source: "https://facade.test/tos"}
    commercial_use: {value: "yes", source: "https://facade.test/tos#c"}
    watermark: IPTC DigitalSourceType trainedAlgorithmicMedia
    watermark_visible: false
    adapter: openai_compatible_images
    model: z-image-turbo
    base_url: http://127.0.0.1:8100/v1
    capacity: unmetered
    tos_reviewed: 2026-10-15
    domains: [facade.test]
"""
LORA = "c" * 64


def _facade(env: CompileEnv, weights: list[str]) -> list[dict[str, object]]:
    """Point the target at the local facade and answer its requests (respx)."""
    import base64
    import io

    import httpx
    import respx
    from PIL import Image

    (env.studio_root / "providers.yaml").write_text(
        (env.studio_root / "providers.yaml").read_text() + LOCAL_ROW
    )
    target = (env.studio_root / "targets" / "web.yaml").read_text()
    (env.studio_root / "targets" / "web.yaml").write_text(
        target.replace("providers: [fake]", "providers: [local-gpu]")
    )
    sent: list[dict[str, object]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        buf = io.BytesIO()
        Image.new("RGB", (64, 36), (len(sent) * 40, 90, 120)).save(buf, "PNG")
        record = {
            "recipe": {"id": body.get("recipe"), "version": 1, "sha256": "d" * 64},
            "models": [
                {"id": "z-image-turbo", "weights": [{"path": "w", "sha256": w} for w in weights]}
            ],
            "seed": 1000 + len(sent),
            "spdx": "Apache-2.0",
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(buf.getvalue()).decode(),
                        "qontinui_provenance": record,
                    }
                ]
            },
        )

    respx.post("http://127.0.0.1:8100/v1/images/generations").mock(side_effect=answer)
    # The fixture context wraps every adapter in a fake; the facade's own
    # adapter (its wire mocked above) is the one under test here.
    from qontinui_studio.adapters import build_adapter

    ctx = env.context()
    ctx.adapters.adapters["local-gpu"] = build_adapter(ctx.inventory.get("local-gpu"))
    return sent


@pytest.fixture
def imagegen_dir(tmp_path: Path) -> Path:
    from .test_compile_doors import _imagegen

    return _imagegen(tmp_path)


@pytest.mark.usefixtures("respx_mock")
def test_a_facade_binding_records_the_engine_recipe_and_the_lora(
    env: CompileEnv, imagegen_dir: Path
) -> None:
    from qontinui_studio.compiler.inputs import ImagegenManifest

    sent = _facade(env, weights=[LORA, "9" * 64])
    env.imagegen = ImagegenManifest(imagegen_dir)
    env.write_spec(HERO, lora="qontinui-house-v1")
    report = env.run()
    [slot] = report.slots
    assert slot.status == "compiled", slot.reasons
    assert [b["recipe"] for b in sent] == ["hero", "hero"]  # the role's facade recipe
    assert all("third-party logos" in str(b["negative_prompt"]) for b in sent)
    binding = slot.binding
    assert binding is not None and binding["lora_sha256"] == LORA
    assert env.run(dry_run=True).slots[0].status == "fresh"
    # The facade recipe changes: the binding is stale on the recipe axis.
    manifest_py = imagegen_dir / "qontinui_imagegen" / "manifest.py"
    manifest_py.write_text(manifest_py.read_text().replace("d" * 64, "f" * 64))
    env.imagegen = ImagegenManifest(imagegen_dir)
    plan = env.run(dry_run=True)
    assert plan.slots[0].status == "planned" and "recipe changed" in plan.slots[0].reasons
    # With no manifest to read, the recipe axis is UNKNOWN — never fresh.
    env.imagegen = ImagegenManifest(Path("/nonexistent"))
    unknown = env.run(dry_run=True)
    assert unknown.slots[0].status == "unknown" and unknown.exit_code() == 8
    assert any("UNKNOWN" in r for r in unknown.slots[0].reasons)


@pytest.mark.usefixtures("respx_mock")
def test_a_candidate_that_did_not_apply_the_lora_is_never_bound(
    env: CompileEnv, imagegen_dir: Path
) -> None:
    from qontinui_studio.compiler.inputs import ImagegenManifest

    _facade(env, weights=["9" * 64])
    env.imagegen = ImagegenManifest(imagegen_dir)
    env.write_spec(HERO, lora="qontinui-house-v1")
    report = env.run()
    [slot] = report.slots
    assert slot.status == "refused" and report.exit_code() == 2
    assert any("does not show those weights applied" in r for r in slot.reasons)
    assert not (env.publish_dir / f"{HERO_FILE}.avif").exists()
    assert len(slot.candidates) == 2 and all(c["usable"] is False for c in slot.candidates)
    # Every candidate still went to the store: the store holds what was made.
    assert [u["kind"] for u in env.store.uploads] == ["candidate", "candidate"]


def test_an_unconfirmed_store_upload_is_reported_not_hidden(env: CompileEnv) -> None:
    env.store.answer = "unknown"
    env.write_spec(HERO)
    report = env.run()
    [slot] = report.slots
    # The bytes still land in the checkout (they travel in the PR); the store's
    # silence is reported as UNKNOWN, and nothing is proposed for an upload the
    # store never confirmed.
    assert slot.status == "compiled"
    assert all(c["store"]["status"] == "unknown" and "proposal" not in c for c in slot.candidates)
    assert len(slot.candidates) == 2
    assert all(d["store"]["status"] == "unknown" for d in slot.derivatives)
    assert env.store.proposals == []
    assert slot.pick is not None and slot.pick["store"]["status"] == "unknown"


def test_a_site_file_may_not_claim_to_be_a_checkout(studio_root: Path) -> None:
    from qontinui_studio.sites import load_site

    path = studio_root / "sites" / "cafe.yaml"
    path.write_text(path.read_text() + "checkout: true\n")
    with pytest.raises(ConfigError, match="never configured"):
        load_site(path)


# ---- the consumer's reader: qontinui-web <SlotImage> (Phase 14) -------------------

# The formats qontinui-web's <SlotImage> serves (frontend/src/lib/image-slots/manifest.ts).
WEB_FORMATS = ("png", "jpeg", "webp", "avif", "svg")


def web_resolve(manifest: dict[str, object], slot: str) -> list[str]:
    """A port of qontinui-web's readStudioManifest + servableFiles: the file
    names <SlotImage slot> would serve, most widely decodable first."""
    assert isinstance(manifest.get("generated_by"), str)
    slots = manifest["slots"]
    assert isinstance(slots, dict)
    entry = slots[slot]
    files, shas = entry["files"], entry["sha256"]
    assert all(isinstance(v, str) for v in [*files.values(), *shas.values()])
    out = []
    for fmt in WEB_FORMATS:
        name, digest = files.get(fmt), shas.get(fmt)
        if name and digest and "/" not in name and "\\" not in name and len(digest) == 64:
            out.append(name)
    return out


def test_the_web_slot_reader_serves_what_compile_writes(env: CompileEnv) -> None:
    """The first qontinui-web slot, `runs.active.idle` (an empty state, a
    dotted id): compiled into the shape <SlotImage> reads, keyed by the slot
    id itself, with a raster file the reader can serve."""
    env.write_spec("runs.active.idle", "empty_state", aspect="1/1")
    report = env.run()
    assert [s.status for s in report.slots] == ["compiled"], report.slots[0].reasons
    manifest = json.loads((env.publish_dir / "studio-manifest.json").read_text())
    assert manifest["generated_by"] == "qontinui-studio"
    entry = manifest["slots"]["runs.active.idle"]
    assert entry["ai_generated"] is True
    assert entry["files"] == {"svg": "runs.active.idle.svg"}
    assert web_resolve(manifest, "runs.active.idle") == ["runs.active.idle.svg"]
    for name in entry["files"].values():
        assert (env.publish_dir / name).is_file()
        assert marker.is_marked(env.publish_dir / name)
    # A diffusion slot: every published format is servable.
    env.write_spec("runs.active.hero", "hero")
    env.run(only={"runs.active.hero"})
    manifest = json.loads((env.publish_dir / "studio-manifest.json").read_text())
    assert web_resolve(manifest, "runs.active.hero") == [
        "runs.active.hero.jpg",
        "runs.active.hero.webp",
        "runs.active.hero.avif",
    ]


# ---- review round 2: input axes, orphans, unreadable bindings, file bytes ----------


def test_an_exemplar_set_change_makes_the_binding_stale(env: CompileEnv) -> None:
    from datetime import date

    from qontinui_studio.library import Library

    # The set needs a member here before any slot naming it can be bound.
    env.write_spec("marketing.home.closing")
    env.run()
    lib = Library(env.context().conn)
    first, second = lib.assets()[:2]
    lib.tag(first.uid, "hero-set", "operator", date(2026, 10, 19))
    env.write_spec(HERO, exemplars="hero-set")
    report = env.run(only={HERO})
    assert [s.status for s in report.slots] == ["compiled"], report.slots[0].reasons
    assert env.run(dry_run=True, only={HERO}).slots[0].status == "fresh"
    lib.tag(second.uid, "hero-set", "operator", date(2026, 10, 19))
    plan = env.run(dry_run=True, only={HERO})
    assert plan.slots[0].status == "planned"
    assert any(r.startswith("exemplar set changed") for r in plan.slots[0].reasons)


def test_an_unseen_exemplar_set_generates_nothing(env: CompileEnv) -> None:
    env.write_spec(HERO, exemplars="never-seen")
    report = env.run()
    assert [s.status for s in report.slots] == ["unknown"]
    assert any("exemplars: UNKNOWN" in r for r in report.slots[0].reasons)
    assert batches(env.context()) == []


def test_an_orphan_entry_and_its_files_go_in_the_same_compile(env: CompileEnv) -> None:
    env.write_spec(HERO)
    env.write_spec("marketing.home.closing")
    env.run()
    (env.slots_dir / "marketing.home.closing.yaml").unlink()
    hand = env.publish_dir / "notes.txt"
    hand.write_text("someone's own file")
    plan = env.run(dry_run=True)
    assert plan.orphans == ["marketing.home.closing"] and plan.retired == []
    report = env.run()
    assert report.orphans == ["marketing.home.closing"] and report.files_written
    manifest = json.loads((env.publish_dir / "studio-manifest.json").read_text())
    assert set(manifest["slots"]) == {HERO_FILE}
    assert not any(p.name.startswith("marketing.home.closing") for p in env.publish_dir.iterdir())
    assert hand.is_file()  # never a file the manifest did not name
    assert env.run().orphans == []


def test_an_orphan_file_with_other_bytes_is_kept(env: CompileEnv) -> None:
    env.write_spec("marketing.home.closing")
    env.write_spec(HERO)
    env.run()
    (env.slots_dir / "marketing.home.closing.yaml").unlink()
    edited = env.publish_dir / "marketing.home.closing.webp"
    edited.write_bytes(b"hand-edited")
    report = env.run()
    assert edited.read_bytes() == b"hand-edited"
    assert any("marketing.home.closing.webp" in k for k in report.kept)


def test_a_lucide_selection_replaces_a_slots_files(env: CompileEnv) -> None:
    env.write_spec("sessions.avatar", "avatar_fallback", aspect="1/1")
    env.write_target("slots:\n  sessions.avatar: {route: diffusion}\n")
    env.run()
    assert (env.publish_dir / "sessions.avatar.webp").is_file()
    icons = env.checkout / "frontend" / "node_modules" / "lucide-react" / "dist" / "esm" / "icons"
    icons.mkdir(parents=True)
    (icons.parent.parent.parent / "package.json").write_text('{"version": "0.562.0"}')
    (icons / "circle-user.js").write_text("export {}")
    env.write_target("slots:\n  sessions.avatar: {icon: circle-user}\n")
    report = env.run()
    assert report.slots[0].status == "selected" and report.files_written
    assert {p.name for p in env.publish_dir.iterdir()} == {"studio-manifest.json"}


def test_an_unreadable_binding_is_unknown_not_rerendered(env: CompileEnv) -> None:
    env.write_spec(HERO)
    env.run()
    path = env.publish_dir / "studio-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["slots"][HERO_FILE]["binding"] = {"asset_sha256": "short"}
    path.write_text(json.dumps(manifest))
    count = len(batches(env.context()))
    report = env.run()
    assert [s.status for s in report.slots] == ["unknown"] and report.exit_code() == 8
    assert len(batches(env.context())) == count


def test_a_file_that_is_not_the_manifests_bytes_is_never_overwritten(env: CompileEnv) -> None:
    env.write_spec(HERO)
    env.run()
    # A file gone: the slot is missing and compiled again.
    (env.publish_dir / f"{HERO_FILE}.webp").unlink()
    plan = env.run(dry_run=True)
    assert plan.slots[0].status == "planned"
    assert any("absent or not the bytes" in r for r in plan.slots[0].reasons)
    # A file holding other bytes: recompiled, but the foreign bytes stay.
    (env.publish_dir / f"{HERO_FILE}.webp").write_bytes(b"other bytes")
    report = env.run()
    assert report.slots[0].status == "refused"
    assert (env.publish_dir / f"{HERO_FILE}.webp").read_bytes() == b"other bytes"


@pytest.mark.usefixtures("respx_mock")
def test_a_lora_change_makes_the_binding_stale(env: CompileEnv, imagegen_dir: Path) -> None:
    from qontinui_studio.compiler.inputs import ImagegenManifest

    _facade(env, weights=[LORA, "9" * 64])
    env.imagegen = ImagegenManifest(imagegen_dir)
    env.write_spec(HERO, lora="qontinui-house-v1")
    assert [s.status for s in env.run().slots] == ["compiled"]
    assert env.run(dry_run=True).slots[0].status == "fresh"
    manifest = imagegen_dir / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace(LORA, "e" * 64))
    env.imagegen = ImagegenManifest(imagegen_dir)
    plan = env.run(dry_run=True)
    assert plan.slots[0].status == "planned"
    assert any(r.startswith("LoRA changed") for r in plan.slots[0].reasons)


@pytest.mark.usefixtures("respx_mock")
def test_an_unknown_recipe_alone_is_unknown_never_fresh(
    env: CompileEnv, imagegen_dir: Path
) -> None:
    from qontinui_studio.compiler.inputs import ImagegenManifest

    _facade(env, weights=["9" * 64])
    env.imagegen = ImagegenManifest(imagegen_dir)
    env.write_spec(HERO)  # no LoRA, no exemplar set: the recipe is the only facade axis
    assert [s.status for s in env.run().slots] == ["compiled"]
    env.imagegen = ImagegenManifest(Path("/nonexistent"))
    report = env.run(dry_run=True)
    [slot] = report.slots
    assert slot.status == "unknown" and report.exit_code() == 8
    assert [r for r in slot.reasons if "UNKNOWN" in r] == [
        r for r in slot.reasons if r.startswith("recipe: UNKNOWN")
    ]


# ---- the authored-SVG validator: nothing hides in <metadata> ----------------------

BYPASSES = {
    "a drawing inside metadata": '<metadata>{x}<path d="M0 0" fill="#ff0000"/></metadata>',
    "a foreign element inside the packet": "<metadata>{xs}</metadata>",
    "a second metadata": "<metadata>{x}</metadata><metadata>{x}</metadata>",
    "metadata off the root": "<g><metadata>{x}</metadata></g><metadata>{x}</metadata>",
    "text inside metadata": "<metadata>loose text{x}</metadata>",
    "an attribute on the packet's description": "<metadata>{xa}</metadata>",
}


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_a_metadata_bypass_is_refused(env: CompileEnv, name: str) -> None:
    from qontinui_studio.compiler.selected import _xmpmeta

    env.write_spec("onboarding.connect", "onboarding", aspect="4/3")
    env.write_target("slots:\n  onboarding.connect: {route: svg}\n")
    spec_hash = env.run().slots[0].spec_sha256
    x = _xmpmeta()
    payload = BYPASSES[name].format(
        x=x,
        xs=x.replace(
            "</rdf:RDF>",
            '<rdf:Description rdf:about=""><foreignObject/></rdf:Description></rdf:RDF>',
        ),
        xa=x.replace('rdf:about=""', 'rdf:about="" onload="x"'),
    )
    svg = SVG_OK.format(h=spec_hash, x=x).replace(f"<metadata>{x}</metadata>", payload)
    (env.slots_dir / "onboarding.connect.svg").write_text(svg)
    assert env.run().slots[0].status == "authored_invalid", name


def test_a_processing_instruction_is_refused(env: CompileEnv) -> None:
    from qontinui_studio.compiler.selected import _xmpmeta

    env.write_spec("onboarding.connect", "onboarding", aspect="4/3")
    env.write_target("slots:\n  onboarding.connect: {route: svg}\n")
    spec_hash = env.run().slots[0].spec_sha256
    ok = SVG_OK.format(h=spec_hash, x=_xmpmeta())
    path = env.slots_dir / "onboarding.connect.svg"
    path.write_text('<?xml version="1.0"?>\n' + ok)  # a declaration is not a PI
    assert env.run().slots[0].status == "authored"
    path.write_text('<?xml-stylesheet href="s.css"?>\n' + ok)
    bad = env.run().slots[0]
    assert bad.status == "authored_invalid"
    assert any("processing instruction" in r for r in bad.reasons)
