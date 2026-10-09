"""Regression tests for the sixth review's reproduction scripts (h1..h3)."""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from qontinui_studio.gates import d7_images
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.sites import Site, forbidden_bridge_reason

from .conftest import FakePreview


def _site(tmp_path: Path, bridge: str) -> Site:
    return Site.model_validate(
        {
            "id": "x",
            "name": "x",
            "kind": "demo",
            "commercial": False,
            "publish_dir": tmp_path / "pub",
            "ui_bridge_url": bridge,
            "preview_url": "http://preview.example/",
            "preview_dir": tmp_path / "prev",
            "preview_manifest_url": "http://preview.example/studio-manifest.json",
            "slots": {"hero": {"width": 1, "height": 1}},
        }
    )


@pytest.mark.parametrize(
    "bridge",
    [
        "http://127。0。0。1:9876/ui-bridge",  # IDNA full stops -> 127.0.0.1
        "http://localtest.me:9876/ui-bridge",  # a public name that resolves to loopback
        "http://LOCALHOST.:9876/x",
        "http://[::ffff:7f00:1]:9876/x",
        "http://0x7f000001:9876/x",
        "http://0:9875/x",
        "http://%31%32%37.0.0.1:9876/x",  # does not resolve: UNKNOWN refuses
    ],
)
def test_h1_the_guard_sees_the_host_httpx_will_request(
    tmp_path: Path, bridge: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = socket.getaddrinfo

    def dns(host: str, *args: object, **kwargs: object) -> object:
        if host == "localtest.me":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return real(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    with pytest.raises(ValueError, match="runner/supervisor|does not resolve"):
        _site(tmp_path, bridge)
    # The guard judges the same host:port httpx would connect to.
    assert httpx.URL(bridge).port in (9875, 9876)
    assert forbidden_bridge_reason(bridge) is not None


def test_h2_a_picture_slot_passes_with_one_rendered_format(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], preview: FakePreview
) -> None:
    # The fake bridge models <picture>: ONE element per slot (avif chosen),
    # webp only as the <img> fallback the browser never fetched.
    out = run_brief(make_context(), write_brief())
    assert out.status == "published", out.publish and out.publish.failures


def test_h2_the_unrendered_format_is_still_verified_by_fetch(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    preview: FakePreview,
) -> None:
    real_fetch = preview.fetch

    def fetch(url: str) -> bytes:
        if url.endswith("/hero.webp"):
            return b"stale webp from an old deploy"
        return real_fetch(url)

    out = run_brief(make_context(fetch=fetch), write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert {g.gate for g in out.publish.failures} == {"d7_images"}
    assert all("hero.webp is not this batch's bytes" in g.detail for g in out.publish.failures)


def test_h2_picture_sources_must_carry_their_sha(tmp_path: Path) -> None:
    site = Site.model_validate(
        {
            "id": "x",
            "name": "x",
            "kind": "demo",
            "commercial": False,
            "publish_dir": tmp_path / "pub",
            "ui_bridge_url": "http://127.0.0.1:4001/__ui-bridge",
            "preview_url": "http://preview.example/",
            "preview_dir": tmp_path / "prev",
            "preview_manifest_url": "http://preview.example/m.json",
            "slots": {"hero": {"width": 1, "height": 1}},
        }
    )
    files = {"hero.avif": "a" * 64, "hero.webp": "b" * 64}
    snap = {
        "page": {"url": "http://preview.example/?studio_batch=b"},
        "elements": [
            {
                "id": "studio-picture-hero",
                "mediaMetadata": {
                    "src": "/hero.avif?sha=" + "a" * 64,
                    "sources": [{"srcset": "/hero.avif 1x"}],  # no ?sha=: stale cache
                    "naturalWidth": 10,
                    "loadingState": "loaded",
                },
            }
        ],
    }
    g = d7_images(site, snap, {"hero": files}, lambda u: b"")
    assert g.failed and "source /hero.avif is not this batch's hero.avif" in g.detail


def test_h3_d7_waits_for_images_to_settle(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], preview: FakePreview
) -> None:
    preview.pending_snapshots = 3  # images still decoding for the first snapshots
    out = run_brief(make_context(), write_brief())
    assert out.status == "published", out.publish and out.publish.failures


def test_h3_images_still_pending_at_the_deadline_refuse(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], preview: FakePreview
) -> None:
    preview.pending_snapshots = 10_000
    out = run_brief(make_context(), write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert any("did not finish loading" in g.detail for g in out.publish.failures)


def test_h3_lazy_images_are_refused_with_guidance(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    preview: FakePreview,
) -> None:
    real = preview.bridge

    def lazy_bridge(base_url: str):  # type: ignore[no-untyped-def]
        bridge = real(base_url)
        snapshot = bridge.snapshot

        def lazy() -> dict[str, object]:
            snap = snapshot()
            for element in snap["elements"]:  # type: ignore[union-attr]
                element["mediaMetadata"]["loadingState"] = "lazy"  # type: ignore[index]
            return snap

        bridge.snapshot = lazy  # type: ignore[method-assign]
        return bridge

    out = run_brief(make_context(bridge=lazy_bridge), write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert any('loading="eager"' in g.detail for g in out.publish.failures)
