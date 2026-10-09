from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from PIL import Image

from qontinui_studio.gates import (
    d5_licence,
    d5_provenance,
    d6_not_the_real_thing,
    d7_page_health,
    d7_preview_manifest,
)
from qontinui_studio.library import AssetRecord, VariantRecord
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.sites import Site, Slot


@pytest.fixture
def records(tmp_path: Path) -> tuple[AssetRecord, VariantRecord]:
    raw = tmp_path / "raw.png"
    var = tmp_path / "v.webp"
    Image.new("RGB", (8, 8)).save(raw)
    Image.new("RGB", (4, 4)).save(var)
    asset = AssetRecord(
        uid="b-v0-0",
        asset_id="a",
        brief_id="a",
        batch_id="b",
        job_id="b-v0",
        provider="p",
        provider_name="P",
        model="m",
        prompt="x",
        created_at="2026-10-09T00:00:00+00:00",
        raw_path=str(raw),
        sha256=sha256_file(raw),
        width=8,
        height=8,
        ai_generated=True,
        licence_commercial_use="yes",
        licence_source="https://x",
        licence_tos_reviewed="2026-10-09",
        automation_allowed="yes",
        watermark="none",
        watermark_visible=False,
    )
    variant = VariantRecord(
        id="v",
        asset_uid=asset.uid,
        site="s",
        slot="hero",
        campaign=None,
        format="webp",
        path=str(var),
        width=4,
        height=4,
        sha256=sha256_file(var),
    )
    return asset, variant


def _site(**kw: object) -> Site:
    base: dict[str, object] = {
        "id": "s",
        "name": "S",
        "kind": "demo",
        "commercial": False,
        "publish_dir": "/tmp/x",
        "slots": {"hero": {"width": 4, "height": 4, "category": "decorative"}},
    }
    base.update(kw)
    return Site.model_validate(base)


def test_provenance_passes_and_detects_tampering(
    records: tuple[AssetRecord, VariantRecord],
) -> None:
    asset, variant = records
    assert d5_provenance(asset, variant).outcome == "pass"
    Path(variant.path).write_bytes(b"tampered")
    assert "does not match" in d5_provenance(asset, variant).detail
    assert d5_provenance(replace(asset, model=""), variant).failed


@pytest.mark.parametrize(
    ("commercial_use", "reviewed", "site_commercial", "ok"),
    [
        ("yes", "2026-10-09", True, True),
        ("no", "2026-10-09", False, True),
        ("no", "2026-10-09", True, False),
        ("unknown", "2026-10-09", False, False),
        ("yes", None, False, False),
    ],
)
def test_d5_licence(
    records: tuple[AssetRecord, VariantRecord],
    commercial_use: str,
    reviewed: str | None,
    site_commercial: bool,
    ok: bool,
) -> None:
    asset, variant = records
    asset = replace(asset, licence_commercial_use=commercial_use, licence_tos_reviewed=reviewed)
    result = d5_licence(asset, variant, _site(commercial=site_commercial))
    assert (result.outcome == "pass") is ok


def test_d6_rules(records: tuple[AssetRecord, VariantRecord]) -> None:
    asset, variant = records
    deco = Slot(width=4, height=4, category="decorative")
    food = Slot(width=4, height=4, category="food")
    shot = Slot(width=4, height=4, category="product_screenshot")
    real = _site(kind="real_business", commercial=True)
    demo = _site()
    assert d6_not_the_real_thing(asset, variant, real, food, "AI-generated image").failed
    assert d6_not_the_real_thing(
        asset, variant, real, Slot(width=1, height=1, category="premises"), "x"
    ).failed
    assert d6_not_the_real_thing(asset, variant, real, deco, "AI-generated image").outcome == "pass"
    assert d6_not_the_real_thing(asset, variant, demo, shot, "Illustrative").failed
    assert d6_not_the_real_thing(asset, variant, demo, deco, "").failed
    assert d6_not_the_real_thing(asset, variant, demo, deco, "Illustrative").outcome == "pass"
    assert demo.label_for_ai() == "Illustrative image (AI-generated)"
    assert real.label_for_ai() == "AI-generated image"


def test_page_health_gate() -> None:
    assert d7_page_health(_site()).outcome == "not_configured"
    assert d7_page_health(_site()).failed
    site = _site(
        page_health_url="http://127.0.0.1:9876/ui-bridge/control/page-health",
        preview_dir="/tmp/preview",
        preview_manifest_url="http://127.0.0.1:3000/studio-manifest.json",
    )
    for summary in ("OK", "WARNING", "ok"):
        ok = d7_page_health(site, lambda u, s=summary: {"success": True, "data": {"summary": s}})
        assert ok.outcome == "pass"
    for summary in ("HEALTHY", "CRITICAL", "UNKNOWN", ""):
        bad = d7_page_health(site, lambda u, s=summary: {"success": True, "data": {"summary": s}})
        assert bad.failed

    def invalid_url(url: str) -> dict[str, object]:
        raise httpx.InvalidURL("not an http error")

    assert "InvalidURL" in d7_page_health(site, invalid_url).detail

    good = b'{"slots": {}}'
    sha = hashlib.sha256(good).hexdigest()
    assert d7_preview_manifest(site, sha, lambda u: good).outcome == "pass"
    stale = d7_preview_manifest(site, sha, lambda u: b"{}")
    assert stale.failed and "not this batch" in stale.detail
    assert d7_preview_manifest(site, sha, invalid_url).failed
    crit = d7_page_health(site, lambda u: {"success": True, "data": {"summary": "CRITICAL"}})
    assert crit.failed
    assert d7_page_health(site, lambda u: {"success": False}).failed

    def unreachable(url: str) -> dict[str, object]:
        raise httpx.ConnectError("refused")

    down = d7_page_health(site, unreachable)
    assert down.failed and "probe failed" in down.detail


def test_d6_label_wording(records: tuple[AssetRecord, VariantRecord]) -> None:
    asset, variant = records
    deco = Slot(width=4, height=4, category="decorative")
    demo = _site()
    real = _site(kind="real_business", commercial=True)
    assert d6_not_the_real_thing(asset, variant, demo, deco, "Example photo").failed
    assert d6_not_the_real_thing(asset, variant, demo, deco, "ILLUSTRATIVE only").outcome == "pass"
    assert d6_not_the_real_thing(asset, variant, real, deco, "Maintained by us").failed
    assert d6_not_the_real_thing(asset, variant, real, deco, "Made with ai").outcome == "pass"


def test_page_health_waiver_and_refusal() -> None:
    required = _site(page_health="required")
    assert d7_page_health(required).failed
    waived = d7_page_health(_site(page_health="waived"))
    assert waived.outcome == "waived" and not waived.failed
