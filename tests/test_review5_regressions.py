"""Regression tests for the fifth review's reproduction scripts (g1..g7), plus
HttpUiBridge against a real (local, fake) UI Bridge HTTP server."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from qontinui_studio import pipeline
from qontinui_studio.gates import BridgeError, HttpUiBridge
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.publish import unpublish_batch
from qontinui_studio.scheduler import Scheduler
from qontinui_studio.workspace import Workspace

from .conftest import FakeClock, FakePreview


def cafe_brief(studio_root: Path, name: str, prompt: str) -> Path:
    p = studio_root / "briefs" / name
    p.write_text(
        f"id: {name.split('.')[0]}\npurpose: t\ntargets: [{{site: cafe-template, slot: hero}}]\n"
        f'aspect_ratio: "16:9"\nprompt: {prompt}\nprovider_preference: [fake]\n'
    )
    return p


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


def test_g1_a_restore_carries_the_current_sites_label(
    make_context: Callable[..., Context], studio_root: Path, clock: FakeClock
) -> None:
    site = studio_root / "sites" / "cafe.yaml"
    _edit(site, "kind: demo", "kind: product")
    a = run_brief(make_context(), cafe_brief(studio_root, "a.yaml", "A image"))
    assert a.status == "published"
    clock.sleep(5)
    _edit(site, "kind: product", "kind: demo")
    ctx = make_context()
    b = run_brief(ctx, cafe_brief(studio_root, "b.yaml", "B image"))
    unpublish_batch(
        ctx.conn, ctx.sites, b.batch_id, ctx.notifier, ctx.clock, bridge=ctx.bridge, fetch=ctx.fetch
    )
    entry = json.loads((studio_root / "published" / "cafe" / "studio-manifest.json").read_text())[
        "slots"
    ]["hero"]
    required = ctx.sites["cafe-template"].label_for_ai()
    assert entry["batch_id"] == a.batch_id
    assert entry["label"] == required == "Illustrative image (AI-generated)"
    labels = {
        r["label"]
        for r in ctx.conn.execute(
            "SELECT label FROM publications WHERE batch_id = ? AND unpublished_at IS NULL",
            (a.batch_id,),
        )
    }
    assert labels == {required}


@pytest.mark.parametrize(
    "preview_url",
    [
        "http://preview.test/cafe",
        "http://Preview.Test/cafe/index.html",
        "http://preview.test:80/cafe/index.html",
        "http://preview.test/cafe/x/../index.html",
    ],
)
def test_g2_d7_passes_when_the_browser_normalises_the_url(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    preview_url: str,
) -> None:
    _edit(
        studio_root / "sites" / "cafe.yaml",
        "preview_url: http://preview.test/cafe/index.html",
        f"preview_url: {preview_url}",
    )
    out = run_brief(make_context(), write_brief())
    assert out.status == "published", out.publish and out.publish.failures


def test_g3_images_from_another_host_or_missing_formats_refuse(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    preview: FakePreview,
) -> None:
    real = preview.bridge

    def live_host_bridge(base_url: str) -> Any:
        bridge = real(base_url)
        snapshot = bridge.snapshot

        def from_live_host() -> dict[str, Any]:
            snap = snapshot()
            for element in snap["elements"]:
                meta = element["mediaMetadata"]
                meta["src"] = "https://cafe.example.com" + meta["src"].split("?")[0]
            return snap

        bridge.snapshot = from_live_host  # type: ignore[method-assign]
        return bridge

    out = run_brief(make_context(bridge=live_host_bridge), write_brief())
    assert out.status == "refused"
    assert out.publish is not None
    assert {g.gate for g in out.publish.failures} == {"d7_images"}


def test_g3_a_meta_only_slot_is_fetched_and_compared(
    make_context: Callable[..., Context],
    studio_root: Path,
    preview: FakePreview,
) -> None:
    _edit(
        studio_root / "sites" / "cafe.yaml",
        "og: {width: 1200, height: 630, formats: [png]}",
        "og: {width: 1200, height: 630, formats: [png], rendered: false}",
    )
    p = studio_root / "briefs" / "og.yaml"
    p.write_text(
        "id: og\npurpose: t\ntargets: [{site: cafe-template, slot: og}]\n"
        'aspect_ratio: "1200:630"\nprompt: card\nprovider_preference: [fake]\n'
    )
    fetched: list[str] = []
    real_fetch = preview.fetch

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return real_fetch(url)

    out = run_brief(make_context(fetch=fetch), p)
    assert out.status == "published", out.publish and out.publish.failures
    assert "http://preview.test/cafe/og.png" in fetched


def test_g4_runner_guard_resolves_addresses() -> None:
    from qontinui_studio.sites import is_local_host

    for host in (
        "127.0.0.2",
        "::ffff:127.0.0.1",
        "127.1",
        "0.0.0.0",
        "::1",
        "localhost",
        "a.localhost",
    ):
        assert is_local_host(host), host
    for host in ("example.com", "10.255.1.1", "192.0.2.10"):
        assert not is_local_host(host), host


def test_g5_ingest_does_not_hold_the_write_lock_while_encoding(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Workspace(studio_root).db_path
    probes: list[str] = []
    real_export = pipeline.export

    def export_and_probe(*args: Any, **kwargs: Any) -> Path:
        other = sqlite3.connect(db, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            probes.append("free")
        except sqlite3.OperationalError as e:
            probes.append(str(e))
        finally:
            other.close()
        return real_export(*args, **kwargs)

    monkeypatch.setattr(pipeline, "export", export_and_probe)
    out = run_brief(make_context(), write_brief(), publish=False)
    assert out.status == "generated"
    assert probes and set(probes) == {"free"}


@pytest.mark.parametrize(
    "tz",
    [
        "America/Los_Angeles",
        "America/Havana",
        "America/Santiago",
        "Asia/Beirut",
        "Australia/Lord_Howe",
        "Europe/Berlin",
        "Pacific/Apia",
    ],
)
def test_g6_quota_days_and_resets_hold_across_dst(tz: str) -> None:
    class Inventory:
        def get(self, pid: str) -> Any:
            return type("P", (), {"quota_reset_tz": tz})()

    s = Scheduler.__new__(Scheduler)
    s.inventory = Inventory()  # type: ignore[assignment]
    t = datetime(2026, 1, 1, tzinfo=UTC)
    while t < datetime(2027, 1, 1, tzinfo=UTC):
        r = s.next_reset("p", t)
        assert r > t
        assert s.quota_day("p", r) != s.quota_day("p", t)
        assert s.quota_day("p", r - timedelta(seconds=1)) == s.quota_day("p", t)
        assert r - t <= timedelta(hours=25)
        t += timedelta(hours=1, minutes=7)


def test_g7_the_d7_preview_is_exactly_what_goes_live(
    make_context: Callable[..., Context],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    a = run_brief(make_context(), cafe_brief(studio_root, "a.yaml", "A image"))
    assert a.status == "published"
    _edit(
        studio_root / "sites" / "cafe.yaml",
        "hero: {width: 1600, height: 900, formats: [webp, avif]}",
        "hero: {width: 1600, height: 900, formats: [webp]}",
    )
    clock.sleep(5)
    b = run_brief(make_context(), cafe_brief(studio_root, "b.yaml", "B image"))
    assert b.status == "published"

    def visible(d: Path) -> list[str]:
        return sorted(p.name for p in d.iterdir() if not p.name.startswith("."))

    assert visible(studio_root / "published" / "cafe") == ["hero.webp", "studio-manifest.json"]
    assert visible(studio_root.parent / "previews" / "cafe") == visible(
        studio_root / "published" / "cafe"
    )


# ---- HttpUiBridge against a fake UI Bridge HTTP server --------------------------


class _BridgeServer(BaseHTTPRequestHandler):
    state: dict[str, Any]

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)  # the real bridge answers 200 even on failure
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.state["requests"].append((self.path, body))
        if self.path == "/__ui-bridge/control/page/navigate":
            if self.state.get("no_tab"):
                self._send({"success": False, "error": "SDK_DISCONNECTED: No browser connected"})
                return
            self.state["url"] = body["url"]
            self._send({"success": True, "data": {"delivered": True, "executed": False}})
        elif self.path == "/__ui-bridge/control/page-health":
            self._send({"success": True, "data": {"summary": "WARNING", "findings": []}})
        else:
            self._send({"success": False, "error": f"unknown {self.path}"})

    def do_GET(self) -> None:  # noqa: N802
        self.state["requests"].append((self.path, None))
        if self.path == "/__ui-bridge/control/snapshot":
            self._send(
                {
                    "success": True,
                    "data": {"page": {"url": self.state.get("url")}, "elements": []},
                }
            )
        else:
            self._send({"success": False, "error": f"unknown {self.path}"})


@pytest.fixture
def bridge_server() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"requests": []}
    handler = type("H", (_BridgeServer,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/__ui-bridge", state
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.local_http
def test_http_ui_bridge_speaks_the_bridge_wire_format(
    bridge_server: tuple[str, dict[str, Any]],
) -> None:
    base, state = bridge_server
    bridge = HttpUiBridge(base)
    bridge.navigate("http://preview.local/index.html?studio_batch=b")
    assert state["requests"][0] == (
        "/__ui-bridge/control/page/navigate",
        {"url": "http://preview.local/index.html?studio_batch=b", "mode": "hard"},
    )
    assert bridge.snapshot()["page"]["url"] == "http://preview.local/index.html?studio_batch=b"
    assert bridge.page_health()["data"]["summary"] == "WARNING"
    state["no_tab"] = True
    with pytest.raises(BridgeError, match="No browser connected"):
        bridge.navigate("http://preview.local/")
