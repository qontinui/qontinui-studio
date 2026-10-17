"""Shared fixtures for the launch-kit tests: a build-record/1 document in the
shape qontinui-coord's composer emits (nulls + reasons included), a product
definition, and a tiny static "running product" served over HTTP."""

from __future__ import annotations

import copy
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from .conftest import DomNode, FakeBridge, FakePreview, generate_media_id

SLUG = "qontinui-studio"
REPO = "qontinui/qontinui-studio"

DOCUMENT: dict[str, Any] = {
    "schema": "build-record/1",
    "generated_at": "2026-10-09T18:00:00Z",
    "product": {
        "slug": SLUG,
        "title": "qontinui-studio",
        "repos": [REPO],
        "window_start": "2026-10-09T00:00:00Z",
        "window_end": None,
    },
    "work_units": [
        {
            "slug": "2026-10-09-factory-built-product-portfolio-and-launch-kit",
            "title": "Factory-built product portfolio and launch kit",
            "status": "shipped",
            "vetted_at": "2026-10-09T09:00:00Z",
            "shipped_at": "2026-10-09T17:00:00Z",
        }
    ],
    "timeline": [
        {
            "at": "2026-10-09T08:00:00Z",
            "work_unit": "2026-10-09-factory-built-product-portfolio-and-launch-kit",
            "from_status": None,
            "to_status": "draft",
        },
        {
            "at": "2026-10-09T17:00:00Z",
            "work_unit": "2026-10-09-factory-built-product-portfolio-and-launch-kit",
            "from_status": "in_progress",
            "to_status": "shipped",
        },
    ],
    "prs": [
        {
            "repo": REPO,
            "number": 1,
            "title": "feat: asset pipeline",
            "opened_at": "2026-10-09T10:00:00Z",
            "landed_at": "2026-10-09T12:00:00Z",
            "ci_duration_secs": 420,
            "attempts": 2,
        },
        {
            "repo": REPO,
            "number": 2,
            "title": "feat: launch kit",
            "opened_at": "2026-10-09T14:00:00Z",
            "landed_at": "2026-10-09T16:30:00Z",
            "ci_duration_secs": None,
            "attempts": None,
        },
    ],
    "gates": {"registered": 3, "cleared": 2},
    "sessions": {
        "count": None,
        "unknown_reason": "sessions.count: census_provisional",
    },
    "wall_clock_secs": 32400,
    "self_corrections": {"red_ci_fixed": 1, "review_findings_fixed": None, "gate_reopens": None},
    # coord's grammar: "<allowlisted path>: <reason code>" (qontinui-web's
    # validator refuses anything else).
    "unknowns": [
        "prs[1].attempts: not_established",
        "prs[1].ci_duration_secs: not_established",
        "self_corrections.review_findings_fixed: not_established",
        "self_corrections.gate_reopens: door_unavailable",
        "sessions.count: census_provisional",
    ],
}


def wrapper(doc: dict[str, Any], version: int = 1, public_slug: str = SLUG) -> dict[str, Any]:
    """The public route's snapshot wrapper around ``doc``."""
    from qontinui_studio.launch.record import canonical_sha256

    return {
        "public_slug": public_slug,
        "version": version,
        "published_at": "2026-10-09T18:05:00Z",
        "content_sha256": canonical_sha256(doc),
        "document": doc,
    }


def public_client(
    doc: dict[str, Any] | None = None,
    *,
    publication: dict[str, Any] | int | None = None,
    public_status: int = 200,
    bearer: str | None = "test-web-bearer",
    **kw: Any,
) -> Any:
    """A BuildRecordClient on the real web base whose doors are an in-process
    MockTransport:

    * the PUBLIC door serves ``doc`` (default: the fixture document) as a
      published snapshot (or answers ``public_status``);
    * the authed ``/publication`` door answers ``publication`` (default: the
      caller's tenant owns this very snapshot; an int is an HTTP status, e.g.
      404 for "route not deployed")."""
    import httpx

    from qontinui_studio.launch.record import (
        AUTHED_PATH,
        PUBLIC_PATH,
        PUBLICATION_PATH,
        BuildRecordClient,
    )

    body = wrapper(doc if doc is not None else document(), **kw)
    owned: dict[str, Any] | int = (
        publication
        if publication is not None
        else {
            "public_slug": SLUG,
            "owned_by_caller_tenant": True,
            "version": body["version"],
            "content_sha256": body["content_sha256"],
            "unpublished": False,
        }
    )

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == PUBLIC_PATH.format(slug=SLUG):
            if public_status != 200:
                return httpx.Response(public_status, json={"detail": "build_record_not_found"})
            return httpx.Response(200, json=body)
        if path == PUBLICATION_PATH.format(slug=SLUG):
            if request.headers.get("authorization") != f"Bearer {bearer}":
                return httpx.Response(401, json={"detail": "unauthorized"})
            if isinstance(owned, int):
                return httpx.Response(owned, json={"detail": "not found"})
            return httpx.Response(200, json=owned)
        if path == AUTHED_PATH.format(slug=SLUG):
            return httpx.Response(200, json=body["document"])
        return httpx.Response(404, json={"detail": "not found"})

    return BuildRecordClient(
        http=httpx.Client(transport=httpx.MockTransport(handle)), bearer=bearer
    )


def document() -> dict[str, Any]:
    return copy.deepcopy(DOCUMENT)


SITE_HTML = """<!doctype html><html><head><meta charset='utf-8'><title>Demo product</title>
<style>body{font:18px system-ui;margin:40px} .hidden{display:none}</style></head>
<body><h1>Demo product</h1><p>The running product under test.</p>
<a id='more' href='#' onclick="document.getElementById('detail').classList.remove('hidden');return false">More</a>
<div id='detail' class='hidden'><h2>Detail</h2><p>Revealed by a click.</p></div>
</body></html>
"""

CAPTURE_SCRIPT = """
viewport: {width: 640, height: 360}
steps:
  - navigate: /
  - wait: {selector: "h1"}
  - screenshot: {name: home, caption: "The home page", hold: 1}
  - click: "#more"
  - wait: {selector: "#detail"}
  - screenshot: {name: detail, caption: "After a click", hold: 1}
"""


#: The fake preview server/bridge host (see conftest.FakePreview): previews
#: live OUTSIDE the studio root, at ``<root>/../previews/<name>``.
PREVIEW_HOST = "http://preview.test"


def preview_root(root: Path) -> Path:
    return root.parent / "previews"


class LaunchBridge(FakeBridge):
    """The conftest fake bridge, taught to load a launch preview: its media
    elements are the landing page's ``<img>`` tags exactly as the page writes
    them (``<rel>?sha=<sha256>``, resolved against the preview), each decoded
    from the preview dir (naturalWidth 0 when it does not decode)."""

    def snapshot(self) -> dict[str, object]:
        site_dir = self.preview.previews / self.site
        index = site_dir / "index.html"
        if not (site_dir / "launch-manifest.json").is_file() or not index.is_file():
            return super().snapshot()
        if not self.preview.media_discovery:
            return {"page": {"url": self.url}, "elements": [{"id": "a-home", "type": "link"}]}
        elements: list[dict[str, object]] = []
        registered: set[str] = set()
        for tag in re.findall(r"<img [^>]*>", index.read_text()):
            attrs = {k: html.unescape(v) for k, v in re.findall(r"([a-z-]+)='([^']*)'", tag)}
            src = attrs.get("src", "")
            width = 0
            try:
                with Image.open(site_dir / src.split("?", 1)[0]) as img:
                    img.load()
                    width = img.width
            except (OSError, ValueError):
                width = 0
            # A browser does not load an off-screen lazy image; the bridge says so.
            lazy = attrs.get("loading") == "lazy"
            state = "lazy" if lazy else self.preview.loading_state(width)
            mid = generate_media_id(DomNode("img", attrs))
            if mid in registered:  # registerMediaElement: duplicate id -> skipped
                continue
            registered.add(mid)
            elements.append(
                {
                    "id": mid,
                    "category": "media",
                    "mediaMetadata": {
                        # A browser's currentSrc is "" for an <img> without src.
                        "src": f"/{self.site}/{src}" if src else "",
                        "naturalWidth": 0 if lazy else width,
                        "loadingState": state,
                        "mediaType": "image",
                    },
                }
            )
        return {"page": {"url": self.url}, "elements": elements}


class LaunchPreview(FakePreview):
    """conftest.FakePreview (which serves any preview file) with the launch-aware bridge."""

    def bridge(self, base_url: str) -> LaunchBridge:
        site = base_url.removeprefix(f"{PREVIEW_HOST}/").split("/", 1)[0]
        return LaunchBridge(self, site)


def launch_preview(root: Path, summary: str = "OK") -> LaunchPreview:
    return LaunchPreview(preview_root(root), summary)


def preview_ctx(root: Path, summary: str = "OK") -> dict[str, Any]:
    """``make_context`` overrides wiring the D7 bridge + fetch to a fake preview."""
    p = launch_preview(root, summary)
    return {"bridge": p.bridge, "fetch": p.fetch}


def preview_lines(name: str, indent: str = "") -> list[str]:
    """The four D7 preview fields for ``name`` (a site or the launch)."""
    return [
        f"{indent}ui_bridge_url: {PREVIEW_HOST}/{name}/__ui-bridge",
        f"{indent}preview_url: {PREVIEW_HOST}/{name}/index.html",
        f"{indent}preview_dir: ../../previews/{name}",
        f"{indent}preview_manifest_url: {PREVIEW_HOST}/{name}/"
        + ("launch-manifest.json" if name == "launch" else "studio-manifest.json"),
    ]


def write_product(
    root: Path,
    *,
    capture: bool = True,
    brief: bool = False,
    preview: bool = True,
    video_seconds: float = 6,
    commercial: bool = False,
    page_scripts: tuple[str, ...] = (),
) -> Path:
    """products/<slug>.yaml plus its capture script and served static site."""
    products = root / "products"
    products.mkdir(parents=True, exist_ok=True)
    site = root / "demo-site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text(SITE_HTML)
    (products / "capture.yaml").write_text(CAPTURE_SCRIPT)
    lines = [
        f"slug: {SLUG}",
        "title: qontinui-studio",
        "tagline: Launch kits for products the factory ships.",
        "description: A test product description.",
        f"repos: [{REPO}]",
        "homepage: https://github.com/qontinui/qontinui-studio",
        "record: {door: public, public_url: 'https://build.example.test/qontinui-studio'}",
        "publish:",
        "  publish_dir: ../published-launch",
        f"  commercial: {'true' if commercial else 'false'}",
    ]
    if page_scripts:
        lines.append("  page_scripts: " + json.dumps(list(page_scripts)))
    if preview:
        lines += preview_lines("launch", "  ")
    if capture:
        lines += [
            "capture:",
            "  script: capture.yaml",
            "  serve_dir: ../demo-site",
            "  record_video: true",
        ]
    if brief:
        lines += ["brief: ../briefs/launch.yaml", "assets_site: launch-assets"]
    lines.append(f"video_seconds: {video_seconds}")
    path = products / f"{SLUG}.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


LAUNCH_SITE = """
id: launch-assets
name: Launch assets
kind: product
commercial: false
publish_dir: ../published/launch-assets
PREVIEW
slots:
  hero: {width: 800, height: 450, category: decorative, formats: [png]}
  broll-1: {width: 640, height: 360, category: decorative, formats: [png]}
""".replace("PREVIEW", "\n".join(preview_lines("launch-assets")))

LAUNCH_BRIEF = """
id: launch-decor
purpose: launch decoration
targets:
  - {site: launch-assets, slot: hero}
  - {site: launch-assets, slot: broll-1}
aspect_ratio: "16:9"
prompt: An abstract assembly line of light
provider_preference: [fake]
"""


def write_launch_decor(root: Path) -> None:
    (root / "sites" / "launch.yaml").write_text(LAUNCH_SITE)
    (root / "briefs" / "launch.yaml").write_text(LAUNCH_BRIEF)


def playwright_ready() -> str | None:
    """None when Playwright and its Chromium can run here, else the skip reason."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Playwright is not installed (poetry install -E launch)"
    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as e:  # noqa: BLE001 - any launch failure means "cannot run here"
        return f"Playwright cannot launch Chromium: {e}"
    return None


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def use_public_door(monkeypatch: Any) -> None:
    """Point publication's record re-check (current snapshot + tenant
    listing) at :func:`public_client` instead of the network."""
    import qontinui_studio.launch.publish as lp

    monkeypatch.setattr(lp, "default_records_client", lambda product: public_client())
