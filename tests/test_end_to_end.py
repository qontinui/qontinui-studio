"""Plan Phase 2 acceptance: one brief file produces slot-cropped assets for two
template sites with zero manual steps; the D7 gates pass; library rows carry
full provenance."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from qontinui_studio.library import Library
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.publish import unpublish_batch
from qontinui_studio.review import build_review_page


def test_one_brief_two_sites_zero_manual_steps(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    ctx = make_context()
    outcome = run_brief(ctx, write_brief(variants=2))

    assert outcome.status == "published", outcome
    assert outcome.publish is not None
    assert not outcome.publish.failures
    gates = {g.gate for g in outcome.publish.gate_results}
    assert {"d5_provenance", "d5_licence", "d6", "d7_page_health"} <= gates

    # Slot-cropped, exact-size assets on BOTH sites, every declared format.
    expected = {
        studio_root / "published" / "cafe": (1600, 900),
        studio_root / "published" / "barber": (1440, 1080),
    }
    for publish_dir, size in expected.items():
        for ext in ("webp", "avif"):
            with Image.open(publish_dir / f"hero.{ext}") as img:
                assert img.size == size
        manifest = json.loads((publish_dir / "studio-manifest.json").read_text())
        entry = manifest["slots"]["hero"]
        assert entry["ai_generated"] is True
        assert entry["label"] == "Illustrative image (AI-generated)"
        assert entry["licence"]["commercial_use"] == "yes"
        assert entry["batch_id"] == outcome.batch_id

    # Full D5 provenance on every library row.
    lib = Library(ctx.conn)
    assets = lib.assets(outcome.batch_id)
    assert len(assets) == 2  # two variants
    for a in assets:
        assert a.provider == "fake" and a.provider_name == "Fake generator"
        assert a.model == "fake-v1"
        assert a.brief_id == "hero-test"
        assert "An empty cafe interior" in a.prompt and "Warm and minimal." in a.prompt
        assert a.created_at and a.sha256 and Path(a.raw_path).is_file()
        assert a.ai_generated is True
        assert a.licence_commercial_use == "yes"
        assert a.licence_tos_reviewed == "2026-10-09"
        assert a.licence_source == "https://example.test/tos#commercial"
        assert a.watermark == "none"
        slots = {(v.site, v.slot, v.format) for v in lib.variants(a.uid)}
        assert ("cafe-template", "hero", "avif") in slots
        assert ("barber-template", "hero", "webp") in slots
        assert any(v.campaign == "launch" for v in lib.variants(a.uid))

    # The operator is notified, not asked: a notice with the one-step unpublish.
    notice = ctx.workspace.notices_dir / f"{outcome.batch_id}-published.md"
    text = notice.read_text()
    assert f"studio unpublish --batch {outcome.batch_id}" in text
    assert "NOT CONFIGURED: d7_page_health" in text

    page = build_review_page(ctx.conn, ctx.workspace.review_dir, ctx.clock())
    html = page.read_text()
    assert outcome.batch_id in html and "studio unpublish --batch" in html
    assert "approve" not in html.lower()

    removed = unpublish_batch(ctx.conn, ctx.sites, outcome.batch_id, ctx.notifier, ctx.clock)
    assert len(removed) == 4
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()
    manifest = json.loads((studio_root / "published" / "cafe" / "studio-manifest.json").read_text())
    assert "hero" not in manifest["slots"]


def test_non_commercial_licence_refused_for_commercial_site(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    ctx = make_context()
    outcome = run_brief(ctx, write_brief(providers="fake2"))
    assert outcome.status == "refused"
    assert outcome.publish is not None
    failing = [g for g in outcome.publish.failures if g.gate == "d5_licence"]
    assert failing and "barber-template" in failing[0].detail
    # Atomic: nothing from the batch reached EITHER site.
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()
    assert not (studio_root / "published" / "barber" / "hero.webp").exists()
    notice = ctx.workspace.notices_dir / f"{outcome.batch_id}-refused.md"
    assert "d5_licence" in notice.read_text()


def test_page_health_failure_rolls_back(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    site_file = studio_root / "sites" / "cafe.yaml"
    site_file.write_text(
        site_file.read_text()
        + "page_health_url: http://127.0.0.1:9/ui-bridge/control/page-health\n"
    )

    def critical(url: str) -> dict[str, object]:
        return {"success": True, "data": {"summary": "CRITICAL", "findings": []}}

    ctx = make_context(probe=critical)
    outcome = run_brief(ctx, write_brief())
    assert outcome.status == "refused"
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()
    assert not (studio_root / "published" / "barber" / "hero.webp").exists()
