from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest
from PIL import Image

from qontinui_studio import marker, provenance
from qontinui_studio.gates import (
    d5_licence,
    d5_provenance,
    d6_not_the_real_thing,
    d7_images,
    d7_page_health,
    d7_preview_manifest,
    d7_preview_page,
)
from qontinui_studio.library import AssetRecord, VariantRecord
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.sites import Site, Slot


def _sidecar(path: Path) -> dict[str, object]:
    return provenance.build(
        file_sha256=sha256_file(path),
        file_name=path.name,
        role="raw",
        provider_id="p",
        provider_name="P",
        adapter="fake",
        model="m",
        terms={"commercial_use": "yes"},
        prompt="x",
        negative_prompt=None,
        references=[],
        seed=None,
        recipe=None,
        created_at="2026-10-09T00:00:00+00:00",
        job_id="b-v0",
        batch_id="b",
        brief_id="a",
        facade=None,
    )


@pytest.fixture
def records(tmp_path: Path) -> tuple[AssetRecord, VariantRecord]:
    raw = tmp_path / "raw.png"
    var = tmp_path / "v.webp"
    Image.new("RGB", (8, 8)).save(raw, "PNG", **marker.save_options("png"))
    Image.new("RGB", (4, 4)).save(var, "WEBP", **marker.save_options("webp"))
    for path in (raw, var):
        provenance.write(path, _sidecar(path))
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


def test_provenance_requires_the_marker_and_a_matching_sidecar(
    records: tuple[AssetRecord, VariantRecord], tmp_path: Path
) -> None:
    """Plan 2026-10-13 Phase 11 step 4: a generated file without the IPTC
    marker, without a sidecar, or with a sidecar for other bytes fails D5."""
    asset, variant = records
    var = Path(variant.path)
    # An unmarked variant (same recorded digest, so only the marker differs).
    unmarked = tmp_path / "unmarked.webp"
    Image.new("RGB", (4, 4)).save(unmarked, "WEBP")
    provenance.write(unmarked, _sidecar(unmarked))
    bare = replace(variant, path=str(unmarked), sha256=sha256_file(unmarked))
    assert "lacks the AI-generated marker" in d5_provenance(asset, bare).detail
    # No sidecar.
    provenance.sidecar_path(var).unlink()
    assert "no provenance sidecar" in d5_provenance(asset, variant).detail
    # A sidecar that describes other bytes.
    other = _sidecar(var)
    other["file"] = {"name": var.name, "sha256": "0" * 64, "role": "raw"}
    provenance.write(var, other)
    assert "describes other bytes" in d5_provenance(asset, variant).detail
    # An imported asset is not held to it (its file was not made by the studio).
    assert d5_provenance(replace(asset, origin="imported"), bare).outcome == "pass"


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


PREVIEW = {
    "ui_bridge_url": "http://127.0.0.1:3000/__ui-bridge",
    "preview_url": "http://127.0.0.1:3000/index.html",
    "preview_dir": "/tmp/preview",
    "preview_manifest_url": "http://127.0.0.1:3000/studio-manifest.json",
}


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9876/ui-bridge",
        "http://127.0.0.2:9876/ui-bridge",
        "http://[::ffff:127.0.0.1]:9876/ui-bridge",
        "http://127.1:9876/ui-bridge",
        "http://127.0.0.1:9875/ui-bridge",
        "http://localhost:9876/ui-bridge",
        "http://0.0.0.0:9876/ui-bridge",
        "http://[::1]:9875/ui-bridge",
    ],
)
def test_the_runner_and_supervisor_uis_are_never_the_bridge(url: str) -> None:
    with pytest.raises(ValueError, match="runner/supervisor's own UI"):
        _site(**{**PREVIEW, "ui_bridge_url": url})


def test_bridge_guard_allows_other_ports_and_needs_all_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    real = socket.getaddrinfo

    def fake_dns(host: str, *args: object, **kwargs: object) -> object:
        table = {"preview.example": "192.0.2.10", "localtest.me": "127.0.0.1"}
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]
        if host == "nowhere.invalid":
            raise socket.gaierror("no such host")
        return real(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    assert _site(**{**PREVIEW, "ui_bridge_url": "http://127.0.0.1:4001/__ui-bridge"})
    # A remote host on 9876 is fine — it is not this machine.
    assert _site(**{**PREVIEW, "ui_bridge_url": "http://preview.example:9876/__ui-bridge"})
    # A name that RESOLVES to loopback is refused, and so is one that does not
    # resolve at all (UNKNOWN), and the IDNA spelling httpx normalises.
    for url in (
        "http://localtest.me:9876/__ui-bridge",
        "http://nowhere.invalid:9876/__ui-bridge",
        "http://127\u30020\u30020\u30021:9876/__ui-bridge",
        "http://0x7f000001:9876/__ui-bridge",
    ):
        with pytest.raises(ValueError, match="runner/supervisor|does not resolve"):
            _site(**{**PREVIEW, "ui_bridge_url": url})
    with pytest.raises(ValueError, match="needs all of"):
        _site(ui_bridge_url="http://127.0.0.1:3000/__ui-bridge")


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, s: float) -> None:
        self.t += s

    def monotonic(self) -> float:
        return self.t


class Bridge:
    def __init__(self, url_after_nav: str | None, elements: list[dict[str, object]]) -> None:
        self.url_after_nav = url_after_nav
        self.elements = elements
        self.url: str | None = "http://127.0.0.1:3000/stale"
        self.navigated: list[str] = []
        self.fail_snapshots = 0

    def navigate(self, url: str, *, hard: bool = False) -> None:
        self.navigated.append(url)
        self.url = self.url_after_nav if self.url_after_nav != "same" else url

    def snapshot(self) -> dict[str, object]:
        if self.fail_snapshots:
            self.fail_snapshots -= 1
            raise ConnectionError("tab re-registering")
        return {"page": {"url": self.url}, "elements": self.elements}

    def page_health(self) -> dict[str, object]:
        return {"success": True, "data": {"summary": "OK"}}


# The bytes the fake preview serves for every file (``_run``'s default fetch).
SHA = hashlib.sha256(b"").hexdigest()


def _img(
    src: str, width: int = 1600, state: str = "loaded", mid: str = "studio-picture-hero"
) -> dict[str, object]:
    return {
        "id": mid,
        "mediaMetadata": {"src": src, "naturalWidth": width, "loadingState": state},
    }


def _run(site: Site, bridge: Bridge, files: dict[str, dict[str, str]], fetch=None):  # type: ignore[no-untyped-def]
    t = FakeTime()
    return d7_preview_page(
        site,
        "batch-1",
        files,
        lambda _u: bridge,
        fetch or (lambda u: b""),
        sleep=t.sleep,
        monotonic=t.monotonic,
    )


def test_d7_is_bound_to_the_batch_preview_and_checks_images() -> None:
    site = _site(**PREVIEW)
    files = {"hero": {"hero.webp": SHA}}
    good = Bridge("same", [_img(f"/hero.webp?sha={SHA}")])
    assert [g.outcome for g in _run(site, good, files)] == ["pass", "pass", "pass"]
    [nav] = good.navigated
    assert nav.startswith("http://127.0.0.1:3000/index.html?studio_batch=batch-1&studio_attempt=")

    stale = Bridge(None, [_img(f"/hero.webp?sha={SHA}")])  # the tab never moved
    [nav] = _run(site, stale, files)
    assert nav.gate == "d7_navigate" and nav.failed

    broken = Bridge("same", [_img(f"/hero.webp?sha={SHA}", 0, "error")])
    assert _run(site, broken, files)[-1].failed
    missing = Bridge("same", [_img(f"/other.webp?sha={SHA}")])
    last = _run(site, missing, files)[-1]
    assert last.failed and "not one of the slot's preview files" in last.detail
    nothing = Bridge("same", [_img(f"/hero.webp?sha={SHA}", mid="img-illustrative")])
    last = _run(site, nothing, files)[-1]
    assert last.failed and 'data-media-id="studio-picture-hero"' in last.detail


@pytest.mark.parametrize(
    "preview_url",
    [
        "http://127.0.0.1:4000",
        "http://Preview.Local:4000/index.html",
        "http://preview.local:80/index.html",
        "http://preview.local/a/../index.html",
        "http://preview.local/index.html",
    ],
)
def test_d7_navigation_compares_browser_normalised_urls(preview_url: str) -> None:
    from .conftest import browser_href

    manifest = urljoin(preview_url, "studio-manifest.json")
    site = _site(**{**PREVIEW, "preview_url": preview_url, "preview_manifest_url": manifest})
    bridge = Bridge(None, [_img(f"{urljoin(manifest, 'hero.webp')}?sha={SHA}")])
    bridge.navigate = lambda url, hard=False: setattr(bridge, "url", browser_href(url))  # type: ignore[method-assign]
    results = _run(site, bridge, {"hero": {"hero.webp": SHA}})
    assert [g.outcome for g in results] == ["pass", "pass", "pass"], results


def test_d7_retries_while_the_tab_re_registers() -> None:
    site = _site(**PREVIEW)
    bridge = Bridge("same", [_img(f"/hero.webp?sha={SHA}")])
    bridge.fail_snapshots = 3
    assert [g.outcome for g in _run(site, bridge, {"hero": {"hero.webp": SHA}})] == [
        "pass",
        "pass",
        "pass",
    ]


def test_d7_refuses_a_page_on_another_origin() -> None:
    site = _site(**PREVIEW)
    other = Bridge("http://127.0.0.1:9876/index.html?studio_batch=batch-1", [])
    [nav] = _run(site, other, {"hero": {"hero.webp": SHA}})
    assert nav.failed and "not the preview origin" in nav.detail


def test_d7_images_must_come_from_the_preview_with_this_batchs_sha() -> None:
    site = _site(**PREVIEW)
    files = {"hero": {"hero.webp": SHA, "hero.avif": "b" * 64}}
    snap = {
        "page": {"url": "http://127.0.0.1:3000/index.html?studio_batch=b"},
        "elements": [
            _img("https://cafe.example.com/img/hero.webp"),
            _img("https://cdn.other.example/hero.avif?v=old"),
        ],
    }
    g = d7_images(site, snap, files, lambda u: b"")
    assert g.failed and "not one of the slot's preview files" in g.detail
    snap["elements"] = [_img(f"/hero.webp?sha={SHA}")]  # avif fetched: wrong bytes
    g = d7_images(site, snap, files, lambda u: b"")
    assert g.failed and "hero.avif" in g.detail
    # The right basename in the WRONG directory is not the slot's file.
    snap["elements"] = [_img(f"/images/hero.webp?sha={SHA}")]
    g = d7_images(site, snap, files, lambda u: b"")
    assert g.failed and "not one of the slot's preview files" in g.detail
    snap["elements"] = [
        _img("/hero.webp?sha=" + "c" * 64),
        _img("/hero.avif?sha=" + "b" * 64, mid="studio-img-hero"),
    ]
    g = d7_images(site, snap, files, lambda u: b"")
    assert g.failed and "not this batch's hero.webp" in g.detail


def test_d7_meta_only_slots_are_verified_by_fetch() -> None:
    site = _site(
        **PREVIEW,
        slots={
            "hero": {"width": 4, "height": 4, "category": "decorative"},
            "og": {"width": 4, "height": 4, "category": "decorative", "rendered": False},
        },
    )
    data = b"og-bytes"
    sha = hashlib.sha256(data).hexdigest()
    snap = {"page": {"url": "http://127.0.0.1:3000/index.html"}, "elements": []}
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return data

    assert d7_images(site, snap, {"og": {"og.png": sha}}, fetch).outcome == "pass"
    assert fetched == ["http://127.0.0.1:3000/og.png"]
    assert d7_images(site, snap, {"og": {"og.png": "0" * 64}}, fetch).failed


def test_page_health_gate() -> None:
    assert d7_page_health(_site()).outcome == "not_configured"
    assert d7_page_health(_site()).failed
    site = _site(**PREVIEW)
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


def _observation(status: str, summary: object = None) -> dict[str, object]:
    """A page-health answer in UI Bridge's Observation envelope, as
    @qontinui/ui-bridge 0.33's `sdk/page-health` producer serves it."""
    data: dict[str, object] = {
        "status": status,
        "provenance": {"producer": {"id": "sdk/page-health"}},
    }
    if summary is not None:
        data["value"] = {"summary": summary, "findings": []}
    return {"success": True, "data": data}


def test_page_health_gate_reads_the_observation_envelope() -> None:
    site = _site(**PREVIEW)
    for summary in ("OK", "WARNING"):
        ok = d7_page_health(site, lambda u, s=summary: _observation("measured", s))
        assert ok.outcome == "pass" and summary in ok.detail
    crit = d7_page_health(site, lambda u: _observation("measured", "CRITICAL"))
    assert crit.failed and "'CRITICAL'" in crit.detail


def test_page_health_gate_refuses_an_unmeasured_observation() -> None:
    """A bridge that could not see the page is UNKNOWN, never clean — even
    when the envelope still carries a value that reads OK."""
    site = _site(**PREVIEW)
    for status in ("unmeasured", "unknown", "error", ""):
        bad = d7_page_health(site, lambda u, s=status: _observation(s, "OK"))
        assert bad.failed and "unreadable page-health answer" in bad.detail
    no_value = d7_page_health(site, lambda u: _observation("measured"))
    assert no_value.failed and "unreadable page-health answer" in no_value.detail
    lower = d7_page_health(site, lambda u: _observation("measured", "ok"))
    assert lower.outcome == "pass"
    for data in ({"value": {"summary": "OK"}}, {"status": "measured", "value": "OK"}):
        odd = d7_page_health(site, lambda u, d=data: {"success": True, "data": d})
        assert odd.failed and "unreadable page-health answer" in odd.detail
    wrapped = {**_observation("measured", "OK"), "success": False}
    refused = d7_page_health(site, lambda u: wrapped)
    assert refused.failed and "unreadable page-health answer" in refused.detail


def test_d6_label_wording(records: tuple[AssetRecord, VariantRecord]) -> None:
    asset, variant = records
    deco = Slot(width=4, height=4, category="decorative")
    demo = _site()
    real = _site(kind="real_business", commercial=True)
    assert d6_not_the_real_thing(asset, variant, demo, deco, "Example photo").failed
    assert d6_not_the_real_thing(asset, variant, demo, deco, "ILLUSTRATIVE only").outcome == "pass"
    assert d6_not_the_real_thing(asset, variant, real, deco, "Maintained by us").failed
    assert d6_not_the_real_thing(asset, variant, real, deco, "Made with ai").outcome == "pass"


def test_page_health_has_no_waiver() -> None:
    required = d7_page_health(_site())
    assert required.failed and required.outcome == "not_configured"


# Free text a probe, a bridge or a preview server answers with: a URL with a
# query and userinfo, an e-mail address and a marker. None of it may reach a
# gate's detail, which ``gate_results.detail`` persists (plan A3.6).
LEAK = "https://op:pw@internal.example:8443/x?token=LEAK-SENTINEL#frag mail op@example.com"


def _assert_omitted(detail: str) -> None:
    assert "LEAK-SENTINEL" not in detail
    assert "op@example.com" not in detail
    assert "internal.example" not in detail
    assert "detail omitted (" in detail


def test_d7_gate_details_never_carry_exception_or_answer_text() -> None:
    site = _site(**PREVIEW)

    def raises(url: str) -> dict[str, object]:
        raise httpx.ConnectError(LEAK)

    probe = d7_page_health(site, raises)
    assert probe.failed and "ConnectError" in probe.detail
    _assert_omitted(probe.detail)

    unreadable = d7_page_health(site, lambda u: {"success": False, "error": LEAK})
    assert unreadable.failed and "unreadable page-health answer" in unreadable.detail
    _assert_omitted(unreadable.detail)

    def fetch_raises(url: str) -> bytes:
        raise httpx.ConnectError(LEAK)

    manifest = d7_preview_manifest(site, SHA, fetch_raises)
    assert manifest.failed and "ConnectError" in manifest.detail
    _assert_omitted(manifest.detail)


def test_d7_navigation_details_never_carry_bridge_exception_text() -> None:
    site = _site(**PREVIEW)
    files = {"hero": {"hero.webp": SHA}}

    def no_bridge(_url: str) -> Bridge:
        raise ConnectionError(LEAK)

    t = FakeTime()
    [nav] = d7_preview_page(
        site, "batch-1", files, no_bridge, lambda u: b"", sleep=t.sleep, monotonic=t.monotonic
    )
    assert nav.failed and "bridge unavailable: ConnectionError" in nav.detail
    _assert_omitted(nav.detail)

    # The page loads, then every later snapshot fails: the settle loop's error.
    class DropsAfterLoad(Bridge):
        def snapshot(self) -> dict[str, object]:
            if self.navigated and getattr(self, "_loaded", False):
                raise ConnectionError(LEAK)
            self._loaded = True
            return super().snapshot()

    results = _run(site, DropsAfterLoad("same", []), files)
    last = results[-1]
    assert last.failed and "bridge unavailable: ConnectionError" in last.detail
    _assert_omitted(last.detail)


def test_a_rejected_page_health_summary_is_printed_only_as_a_status_word() -> None:
    site = _site(**PREVIEW)
    crit = d7_page_health(site, lambda u: {"success": True, "data": {"summary": "CRITICAL"}})
    assert crit.failed and "'CRITICAL'" in crit.detail
    free = d7_page_health(site, lambda u: {"success": True, "data": {"summary": LEAK}})
    assert free.failed
    _assert_omitted(free.detail)


def test_a_foreign_page_url_is_shown_without_its_query_or_userinfo() -> None:
    site = _site(**PREVIEW)
    [nav] = _run(site, Bridge(LEAK, []), {"hero": {"hero.webp": SHA}})
    assert nav.failed and "/x' (query omitted)" in nav.detail
    assert "LEAK-SENTINEL" not in nav.detail and "op@example.com" not in nav.detail
    assert "op:pw" not in nav.detail

    class MovesAway(Bridge):
        def snapshot(self) -> dict[str, object]:
            snap = super().snapshot()
            if getattr(self, "_loaded", False):
                snap["page"] = {"url": LEAK}
            self._loaded = True
            return snap

    moved = _run(site, MovesAway("same", []), {"hero": {"hero.webp": SHA}})[-1]
    assert moved.failed and "page moved to" in moved.detail
    assert "LEAK-SENTINEL" not in moved.detail and "op:pw" not in moved.detail


def test_an_unparseable_bridge_url_fails_the_gate_without_raising_its_text() -> None:
    site = _site(**PREVIEW)
    bad = "http://127.0.0.1:LEAK-SENTINEL/index.html"
    [nav] = _run(site, Bridge(bad, []), {"hero": {"hero.webp": SHA}})
    assert nav.failed
    _assert_omitted(nav.detail)

    class MovesToUnparseable(Bridge):
        def snapshot(self) -> dict[str, object]:
            snap = super().snapshot()
            if getattr(self, "_loaded", False):
                snap["page"] = {"url": bad}
            self._loaded = True
            return snap

    moved = _run(site, MovesToUnparseable("same", []), {"hero": {"hero.webp": SHA}})[-1]
    assert moved.failed and "page moved to" in moved.detail
    assert "LEAK-SENTINEL" not in moved.detail
