"""Screenshots and a walkthrough of the RUNNING product (D6: real pixels only).

The UI Bridge serves screenshots but has no video route, so capture is driven
from a declarative action script with Playwright as the headless browser — its
one sanctioned use here. Playwright's own video recording produces the
walkthrough clip; when recording is off or fails, ``video.stitch_stills`` turns
the screenshots into one with ffmpeg.

An action script (YAML)::

    viewport: {width: 1280, height: 720}
    steps:
      - navigate: /                       # relative to the capture base URL
      - wait: {selector: "h1"}            # or {ms: 500}
      - screenshot: {name: home, caption: "The home page"}
      - click: "a#start"
      - fill: {selector: "input[name=q]", value: "pizza"}
      - screenshot: {name: results, full_page: true}

Playwright is the optional ``launch`` extra; without it :class:`PlaywrightUnavailable`.
"""

from __future__ import annotations

import functools
import http.server
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from qontinui_studio.errors import ConfigError, StudioError
from qontinui_studio.gates import origin
from qontinui_studio.postprocess import sha256_file


class CaptureError(StudioError):
    """Base class for a capture failure."""


class PlaywrightUnavailable(CaptureError):
    """Playwright (``poetry install -E launch``) or its browser
    (``playwright install chromium``) is not installed."""


class CaptureFailed(CaptureError):
    """A step of the action script failed against the running product."""


# ---- the action script ---------------------------------------------------------


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WaitSpec(_Step):
    selector: str | None = None
    ms: int | None = Field(default=None, ge=0, le=60_000)

    @model_validator(mode="after")
    def _one(self) -> WaitSpec:
        if (self.selector is None) == (self.ms is None):
            raise ValueError("wait needs exactly one of selector or ms")
        return self


class FillSpec(_Step):
    selector: str
    value: str


class ShotSpec(_Step):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    caption: str | None = None
    full_page: bool = False
    # Seconds this screenshot holds when stills are stitched into a walkthrough.
    hold: float = Field(default=3.0, gt=0, le=30)


class Step(_Step):
    """Exactly one action per step."""

    navigate: str | None = None
    click: str | None = None
    wait: WaitSpec | None = None
    fill: FillSpec | None = None
    screenshot: ShotSpec | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Step:
        set_fields = [
            k
            for k in ("navigate", "click", "wait", "fill", "screenshot")
            if getattr(self, k) is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(f"each step is exactly one action, got {set_fields or 'none'}")
        return self


class Viewport(_Step):
    width: int = Field(default=1280, gt=0, le=3840)
    height: int = Field(default=720, gt=0, le=2160)


class ActionScript(_Step):
    viewport: Viewport = Field(default_factory=Viewport)
    timeout_ms: int = Field(default=15_000, gt=0, le=120_000)
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _has_shot(self) -> ActionScript:
        names = [s.screenshot.name for s in self.steps if s.screenshot is not None]
        if not names:
            raise ValueError("an action script must take at least one screenshot")
        if len(names) != len(set(names)):
            raise ValueError(f"screenshot names must be unique: {names}")
        return self


def load_script(path: Path) -> ActionScript:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        return ActionScript.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e


# ---- results ---------------------------------------------------------------------


@dataclass(frozen=True)
class Screenshot:
    name: str
    path: Path
    sha256: str
    caption: str | None
    hold: float
    url: str


@dataclass
class CaptureResult:
    base_url: str
    screenshots: list[Screenshot] = field(default_factory=list)
    video: Path | None = None
    video_note: str | None = None


# ---- the local static server ----------------------------------------------------------


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return


@contextmanager
def serve_directory(directory: Path) -> Iterator[str]:
    """Serve ``directory`` on an ephemeral 127.0.0.1 port; yields the base URL."""
    if not directory.is_dir():
        raise ConfigError(f"capture serve_dir {directory} does not exist")
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---- Playwright ---------------------------------------------------------------------------


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise PlaywrightUnavailable(
            "walkthrough capture needs the optional Playwright dependency: "
            "poetry install -E launch && poetry run playwright install chromium"
        ) from e
    return sync_playwright


def capture(
    script: ActionScript, base_url: str, out_dir: Path, record_video: bool = True
) -> CaptureResult:
    """Run ``script`` against ``base_url`` in headless Chromium; write
    ``<out_dir>/<name>.png`` per screenshot step and, when ``record_video``,
    ``<out_dir>/walkthrough.webm``."""
    sync_playwright = _import_playwright()
    from playwright.sync_api import Error as PlaywrightError

    out_dir.mkdir(parents=True, exist_ok=True)
    video_tmp = out_dir / ".video-tmp"
    result = CaptureResult(base_url=base_url)
    size = {"width": script.viewport.width, "height": script.viewport.height}
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PlaywrightError as e:
                raise PlaywrightUnavailable(
                    f"Playwright could not launch Chromium (poetry run playwright install chromium): {e}"
                ) from e
            try:
                ctx_args: dict[str, Any] = {"viewport": size}
                if record_video:
                    ctx_args["record_video_dir"] = str(video_tmp)
                    ctx_args["record_video_size"] = size
                context = browser.new_context(**ctx_args)
                context.route("**/*", _same_origin_only(base_url))
                context.set_default_timeout(script.timeout_ms)
                page = context.new_page()
                video = page.video if record_video else None
                for index, step in enumerate(script.steps):
                    try:
                        _run_step(page, step, base_url, out_dir, result)
                    except PlaywrightError as e:
                        raise CaptureFailed(f"step {index + 1} ({_describe(step)}): {e}") from e
                context.close()
                if video is not None:
                    recorded = Path(video.path())
                    if recorded.is_file() and recorded.stat().st_size > 0:
                        dest = out_dir / "walkthrough.webm"
                        shutil.move(str(recorded), dest)
                        result.video = dest
                    else:
                        result.video_note = "Playwright recorded no video file"
                elif not record_video:
                    result.video_note = "video recording disabled for this product"
            finally:
                browser.close()
    finally:
        shutil.rmtree(video_tmp, ignore_errors=True)
    return result


def _describe(step: Step) -> str:
    for key in ("navigate", "click", "wait", "fill", "screenshot"):
        value = getattr(step, key)
        if value is not None:
            return f"{key} {value}"
    return "?"


def navigate_target(base_url: str, navigate: str) -> str:
    """Where a ``navigate`` step goes: resolved against the product's base URL
    and refused (:class:`CaptureFailed`) unless it is http(s) on the product's
    own origin — never a ``file:``, ``javascript:`` or another site."""
    url = urljoin(base_url, navigate)
    if urlparse(url).scheme not in ("http", "https") or origin(url) != origin(base_url):
        raise CaptureFailed(
            f"navigate {navigate!r} resolves to {url}, which is not on the product's origin "
            f"({base_url})"
        )
    return url


def _same_origin_only(base_url: str) -> Callable[[Any], None]:
    """A Playwright route handler: requests to any other http(s) origin are
    aborted, so the captured page shows only the product's own content."""

    def handle(route: Any) -> None:
        url = route.request.url
        if urlparse(url).scheme in ("http", "https") and origin(url) != origin(base_url):
            route.abort()
        else:
            route.continue_()

    return handle


def _run_step(page: Any, step: Step, base_url: str, out_dir: Path, result: CaptureResult) -> None:
    if step.navigate is not None:
        url = navigate_target(base_url, step.navigate)
        page.goto(url, wait_until="load")
    elif step.click is not None:
        page.click(step.click)
    elif step.fill is not None:
        page.fill(step.fill.selector, step.fill.value)
    elif step.wait is not None:
        if step.wait.selector is not None:
            page.wait_for_selector(step.wait.selector, state="visible")
        else:
            page.wait_for_timeout(step.wait.ms or 0)
    elif step.screenshot is not None:
        shot = step.screenshot
        if origin(page.url) != origin(base_url):
            # A click or a redirect left the product: never publish pixels of
            # another site (or of a local file a link pointed at).
            raise CaptureFailed(
                f"screenshot '{shot.name}': the page is at {page.url}, outside the product "
                f"({base_url}); refusing to capture it"
            )
        path = out_dir / f"{shot.name}.png"
        page.screenshot(path=str(path), full_page=shot.full_page)
        result.screenshots.append(
            Screenshot(shot.name, path, sha256_file(path), shot.caption, shot.hold, page.url)
        )
