"""Action-script validation, the typed Playwright-absent error, and the ffmpeg
stills path (the walkthrough when video recording is unavailable)."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from qontinui_studio.errors import ConfigError
from qontinui_studio.launch.capture import (
    ActionScript,
    PlaywrightUnavailable,
    capture,
    load_script,
)
from qontinui_studio.launch.video import Still, media_duration, stills_clip

from .launch_support import CAPTURE_SCRIPT, needs_ffmpeg, playwright_ready


def test_script_loads(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(CAPTURE_SCRIPT)
    script = load_script(path)
    assert script.viewport.width == 640
    assert [s.screenshot.name for s in script.steps if s.screenshot] == ["home", "detail"]


@pytest.mark.parametrize(
    ("steps", "match"),
    [
        ([{"navigate": "/"}], "at least one screenshot"),
        ([{"navigate": "/", "click": "a"}, {"screenshot": {"name": "a"}}], "exactly one action"),
        ([{"wait": {}}, {"screenshot": {"name": "a"}}], "exactly one of selector or ms"),
        ([{"screenshot": {"name": "a"}}, {"screenshot": {"name": "a"}}], "unique"),
        ([{"hover": "x"}], "hover"),
    ],
)
def test_bad_scripts_are_config_errors(tmp_path: Path, steps: list[Any], match: str) -> None:
    import yaml

    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    with pytest.raises(ConfigError, match=match):
        load_script(path)


def test_missing_playwright_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_playwright(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    script = ActionScript.model_validate({"steps": [{"screenshot": {"name": "a"}}]})
    with pytest.raises(PlaywrightUnavailable, match="poetry install -E launch"):
        capture(script, "http://127.0.0.1:9/", tmp_path)


@needs_ffmpeg
def test_stills_are_stitched_into_a_walkthrough(tmp_path: Path) -> None:
    frames = []
    for i, (color, size) in enumerate([("red", (640, 360)), ("blue", (300, 900))]):
        path = tmp_path / f"{i}.png"
        Image.new("RGB", size, color).save(path)
        frames.append(Still(path, 1.0))
    clip = stills_clip(frames, tmp_path / "walk.mp4", 320, 180)
    assert abs(media_duration(clip) - 2.0) < 0.3


@pytest.mark.parametrize(
    "navigate",
    [
        "file:///home/someone/.qontinui/coord-device-jwt",
        "//evil.example/x",
        "https://competitor.example/",
        "javascript:alert(1)",
        "chrome://settings",
        "http://127.0.0.1:43112/other-port",
    ],
)
def test_review_nav_targets_off_the_product_are_refused(navigate: str) -> None:
    """Review finding 5 (scripts/nav.py): a navigate step resolves against the
    product's base URL and must stay on its origin."""
    from qontinui_studio.launch.capture import CaptureFailed, navigate_target

    with pytest.raises(CaptureFailed, match="not on the product's origin"):
        navigate_target("http://127.0.0.1:43111/", navigate)
    assert navigate_target("http://127.0.0.1:43111/", "/a?b=1") == "http://127.0.0.1:43111/a?b=1"
    assert navigate_target("http://127.0.0.1:43111/x/", "y") == "http://127.0.0.1:43111/x/y"


_PW_SKIP = playwright_ready()
needs_playwright = pytest.mark.skipif(_PW_SKIP is not None, reason=_PW_SKIP or "")


@needs_playwright
def test_review_cap_a_file_url_is_never_captured(tmp_path: Path) -> None:
    """Review finding 5 (scripts/cap.py): a script navigating to a local file
    (an operator secret) is refused before Chromium loads it."""
    from qontinui_studio.launch.capture import CaptureFailed, serve_directory

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>product</h1>")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN=do-not-publish")
    script = ActionScript.model_validate(
        {"steps": [{"navigate": secret.as_uri()}, {"screenshot": {"name": "home"}}]}
    )
    with serve_directory(site) as base, pytest.raises(CaptureFailed, match="origin"):
        capture(script, base, tmp_path / "out", record_video=False)
    assert not (tmp_path / "out" / "home.png").exists()


@needs_playwright
def test_review_cap_a_click_that_leaves_the_product_is_not_captured(tmp_path: Path) -> None:
    """A link that navigates to another origin: the screenshot is refused; and
    while on the product, a request to another origin (an image) is blocked —
    the other server never receives it."""
    import http.server
    import threading

    from qontinui_studio.launch.capture import CaptureFailed, serve_directory

    hits: list[str] = []

    class Counting(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>someone else</h1>")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Counting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    other_base = f"http://127.0.0.1:{server.server_address[1]}/"
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        f"<h1>product</h1><img src='{other_base}pixel.png'><a id='out' href='{other_base}'>x</a>"
    )
    try:
        home_only = ActionScript.model_validate(
            {"steps": [{"navigate": "/"}, {"wait": {"ms": 300}}, {"screenshot": {"name": "home"}}]}
        )
        with serve_directory(site) as base:
            capture(home_only, base, tmp_path / "out", record_video=False)
        assert hits == []  # the off-origin image request was aborted
        leave = ActionScript.model_validate(
            {
                "steps": [
                    {"navigate": "/"},
                    {"click": "#out"},
                    {"wait": {"ms": 300}},
                    {"screenshot": {"name": "away"}},
                ]
            }
        )
        with serve_directory(site) as base, pytest.raises(CaptureFailed):
            capture(leave, base, tmp_path / "out2", record_video=False)
        assert not (tmp_path / "out2" / "away.png").exists()
    finally:
        server.shutdown()
        server.server_close()


@needs_ffmpeg
def test_review_concat_paths_with_quotes(tmp_path: Path) -> None:
    """Review finding 10 (scripts/concat.py): a clip path containing ``'``."""
    from qontinui_studio.launch.video import concat

    d = tmp_path / "o'brien"
    d.mkdir()
    Image.new("RGB", (64, 64), (9, 9, 9)).save(d / "f.png")
    a = stills_clip([Still(d / "f.png", 0.5)], d / "a.mp4", 64, 64)
    out = concat([a, a], d / "out.mp4")
    assert abs(media_duration(out) - 1.0) < 0.3
