from __future__ import annotations

import json
import posixpath
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from PIL import Image

from qontinui_studio.adapters import AdapterSet, build_adapters
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.notify import FileNotifier
from qontinui_studio.pipeline import Context
from qontinui_studio.providers import load_providers
from qontinui_studio.sites import load_sites
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

PROVIDERS = """
note: test inventory
operator_accounts_owed: false
providers:
  - id: fake
    provider: Fake generator
    plan: test
    api_available: {value: "yes", source: "https://example.test/api"}
    automation_allowed: {value: "yes", source: "https://example.test/tos"}
    commercial_use: {value: "yes", source: "https://example.test/tos#commercial"}
    watermark: none
    watermark_visible: false
    adapter: fake
    model: fake-v1
    daily_quota: 10
    tos_reviewed: 2026-10-09
    domains: [example.test]
  - id: fake2
    provider: Second fake generator
    plan: test
    api_available: {value: "yes", source: "https://example2.test/api"}
    automation_allowed: {value: "yes", source: "https://example2.test/tos"}
    commercial_use: {value: "no", source: "https://example2.test/tos#noncommercial"}
    watermark: none
    adapter: fake
    model: fake-v2
    daily_quota: 10
    tos_reviewed: 2026-10-09
    domains: [example2.test]
"""

CAFE = """
id: cafe-template
name: Cafe template
kind: demo
commercial: false
publish_dir: ../published/cafe
ui_bridge_url: http://preview.test/cafe/__ui-bridge
preview_url: http://preview.test/cafe/index.html
preview_dir: ../../previews/cafe
preview_manifest_url: http://preview.test/cafe/studio-manifest.json
slots:
  hero: {width: 1600, height: 900, formats: [webp, avif]}
  og: {width: 1200, height: 630, formats: [png]}
"""

BARBER = """
id: barber-template
name: Barber template
kind: demo
commercial: true
publish_dir: ../published/barber
ui_bridge_url: http://preview.test/barber/__ui-bridge
preview_url: http://preview.test/barber/index.html
preview_dir: ../../previews/barber
preview_manifest_url: http://preview.test/barber/studio-manifest.json
slots:
  hero: {width: 1440, height: 1080, formats: [webp, avif]}
"""

BRIEF = """
id: hero-test
purpose: test hero
targets:
  - {site: cafe-template, slot: hero}
  - {site: barber-template, slot: hero}
  - {campaign: launch}
aspect_ratio: "16:9"
prompt: An empty cafe interior in warm light
style_guide: style.md
provider_preference: [{providers}]
variants: {variants}
"""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Tests never touch the network: provider HTTP is mocked with respx, which
    intercepts at the httpx transport, so any real socket connect is a bug.
    A test marked ``local_http`` may connect to the loopback address only (to a
    server the test itself started)."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection
    loopback_ok = request.node.get_closest_marker("local_http") is not None

    def allowed(address: object) -> bool:
        host = address[0] if isinstance(address, tuple) and address else None
        return loopback_ok and host in ("127.0.0.1", "::1", "localhost")

    def connect(self: socket.socket, address: object) -> None:
        if not allowed(address):
            raise RuntimeError("tests must not open network connections")
        real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        if not allowed(address):
            raise RuntimeError("tests must not open network connections")
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    def create_connection(address: object, *args: object, **kwargs: object) -> socket.socket:
        if not allowed(address):
            raise RuntimeError("tests must not open network connections")
        return real_create(address, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)


@pytest.fixture(autouse=True)
def _no_runner_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI reads the local runner's model-gateway state before generating.
    Tests never reach a real runner: by default there is none (``not_declared``,
    the no-runner reading). Gateway tests patch this again or call
    ``gateway.read_gateway_state`` against their own loopback server."""

    def no_runner() -> object:
        from qontinui_studio.gateway import GatewayState

        return GatewayState("not_declared", "no_runner", "test")

    # Patched by dotted name and with raising=False, so SETTING UP the fixture
    # imports nothing from the gateway module and survives a build that has no
    # ``read_gateway_state``. The stub itself DOES import ``GatewayState`` from
    # the code under test, lazily, when the CLI calls it.
    monkeypatch.setattr("qontinui_studio.cli.read_gateway_state", no_runner, raising=False)


@pytest.fixture(autouse=True)
def _coord_dial_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Studio reads coord's ``image_generation`` dial before generating. Tests
    never reach coord: by default the dial answers coord's no-row default
    (``on``), and this machine's ``QONTINUI_IMAGEGEN`` is unset. Off-switch
    tests install their own reader (``Context.imagegen``) or patch this again."""

    def coord_default() -> object:
        from qontinui_studio.imagegen_policy import DialReading

        return lambda repo: DialReading("on", "test: coord's no-row default", "none")

    monkeypatch.delenv("QONTINUI_IMAGEGEN", raising=False)
    monkeypatch.setattr(
        "qontinui_studio.imagegen_policy.default_reader", coord_default, raising=False
    )


@pytest.fixture(autouse=True)
def _no_live_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``ui_bridge_wrapper`` row dispatches to the runner on 127.0.0.1:9876 by
    default, and a ``local_http`` test may connect to loopback: point the
    wrapper door at a port nothing listens on, so no test ever reaches (and
    drives a browser through) a real runner. Tests that serve their own runner
    stub set ``STUDIO_RUNNER_URL`` again."""
    from .imagehost import closed_port

    monkeypatch.setenv("STUDIO_RUNNER_URL", f"http://127.0.0.1:{closed_port()}")


def notice_text(notices_dir: Path, batch_id: str, outcome: str) -> str:
    """The newest notice for (batch, outcome); notices are timestamped."""
    matches = sorted(notices_dir.glob(f"{batch_id}-{outcome}-*.md"))
    assert matches, f"no {outcome} notice for {batch_id} in {notices_dir}"
    return matches[-1].read_text()


def debris(publish_dir: Path) -> list[Path]:
    """Hidden staging/backup/journal/tmp files left in a publish dir (the
    persistent lock file is expected and excluded)."""
    return [p for p in publish_dir.iterdir() if p.name.startswith(".") and p.name != ".studio.lock"]


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def studio_root(tmp_path: Path) -> Path:
    root = tmp_path / "studio"
    (root / "sites").mkdir(parents=True)
    (root / "briefs").mkdir()
    (root / "providers.yaml").write_text(PROVIDERS)
    (root / "sites" / "cafe.yaml").write_text(CAFE)
    (root / "sites" / "barber.yaml").write_text(BARBER)
    (root / "briefs" / "style.md").write_text("Warm and minimal.")
    return root


@pytest.fixture
def write_brief(studio_root: Path) -> Callable[..., Path]:
    def _write(providers: str = "fake", variants: int = 1, name: str = "hero.yaml") -> Path:
        path = studio_root / "briefs" / name
        path.write_text(
            BRIEF.replace("{providers}", providers).replace("{variants}", str(variants))
        )
        return path

    return _write


def _async_fakes(adapters: AdapterSet) -> AdapterSet:
    """Make every fake report one pending poll, as an asynchronous provider would."""
    for pid, a in list(adapters.adapters.items()):
        adapters.adapters[pid] = FakeImageAdapter(pid, a.model, pending_polls=1)
    return adapters


def browser_href(url: str) -> str:
    """What a browser reports as ``location.href`` for ``url`` (the WHATWG
    serialisation, for the parts that matter here): lowercase scheme and host,
    the default port dropped, dot segments resolved, an empty path as ``/``."""
    p = urlsplit(url)
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    port = p.port
    if port is not None and port != {"http": 80, "https": 443}.get(scheme):
        host = f"{host}:{port}"
    path = p.path or "/"
    norm = posixpath.normpath(path)
    if path.endswith("/") and norm != "/":
        norm += "/"
    return urlunsplit((scheme, host, norm, p.query, p.fragment))


def slugify(text: str, max_length: int = 30) -> str:
    """UI Bridge media-discovery.ts slugify."""
    text = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    return re.sub(r"\s+", "-", text)[:max_length].rstrip("-")


class DomNode:
    """Just enough DOM for generateMediaId: a tag, its attributes, and its
    parent's children (as tags) for the sibling/child-index fallbacks. (A plain
    class: review harnesses load this file without registering the module,
    which @dataclass cannot handle.)"""

    def __init__(
        self,
        tag: str,
        attrs: dict[str, str] | None = None,
        parent: DomNode | None = None,
        children: list[str] | None = None,
    ) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children = children or []


def generate_media_id(el: DomNode) -> str:
    """A port of UI Bridge ``generateMediaId`` (media-discovery.ts): explicit
    ``data-media-id`` first, then alt / aria-label / title, then the src file
    name, then ancestor context (none here: plain sections), sibling index,
    and finally the child index."""
    explicit = el.attrs.get("data-media-id")
    if explicit:
        return explicit
    prefix = "picture" if el.tag == "picture" else el.tag
    for attr in ("alt", "aria-label", "title"):
        value = el.attrs.get(attr, "").strip()
        if value and slugify(value):
            return f"{prefix}-{slugify(value)}"
    src = el.attrs.get("src")
    if src:
        filename = src.split("/")[-1].split("?")[0].split("#")[0]
        stem = re.sub(r"\.[^.]+$", "", filename)
        if slugify(stem, 20):
            return f"{prefix}-{slugify(stem, 20)}"
    if el.parent is not None:
        same = [c for c in el.parent.children if c == el.tag]
        if len(same) > 1:
            return f"{prefix}-{same.index(el.tag)}"
        return f"{prefix}-child-{el.parent.children.index(el.tag)}"
    return f"{prefix}-root"


def decoded_width(path: Path) -> int:
    """What a browser reports as ``naturalWidth`` for an image file: Pillow's
    decode for a raster, the root ``width`` for an SVG (a browser renders an
    SVG with an intrinsic size at it); 0 when it does not decode."""
    if path.suffix == ".svg":
        from qontinui_studio import marker

        try:
            root = marker.svg_root(path.read_bytes())
        except OSError:
            return 0
        try:
            return int(float(root.get("width", "0"))) if root is not None else 0
        except ValueError:
            return 0
    try:
        with Image.open(path) as img:
            img.load()
            return int(img.width)
    except (OSError, ValueError):
        return 0


class FakeBridge:
    """A fake UI Bridge in front of one site's preview server. ``navigate``
    "loads" a URL and reports it browser-normalised as ``page.url``;
    ``snapshot`` lists one media element per file the preview manifest names,
    with ``src`` carrying the manifest's ``?sha=`` cache-buster (as a site
    template does) and ``naturalWidth`` from actually decoding the file in the
    preview dir (0 when it does not decode)."""

    def __init__(self, preview: FakePreview, site: str) -> None:
        self.preview = preview
        self.site = site
        self.url: str | None = preview.stale_url.get(site)
        # The DOM of the currently loaded document, as the manifest it was
        # rendered from. A HARD navigation loads a new document (re-rendered
        # from the preview as it is now); a client-side navigation only
        # changes the URL and keeps this DOM.
        self.document: bytes | None = None

    def navigate(self, url: str, *, hard: bool = False) -> None:
        """Like the real bridge's pageNavigate: only ``hard`` reloads the
        document; otherwise an app with a client-side router just updates the
        URL and keeps the old DOM."""
        self.preview.navigated.append(url)
        href = browser_href(url)
        if href.startswith(f"http://preview.test/{self.site}") and not self.preview.ignore_navigate:
            self.url = href
            if hard:
                manifest = self.preview.previews / self.site / "studio-manifest.json"
                self.document = manifest.read_bytes() if manifest.is_file() else None

    def snapshot(self) -> dict[str, object]:
        site_dir = self.preview.previews / self.site
        elements: list[dict[str, object]] = []
        manifest_path = site_dir / "studio-manifest.json"
        if not self.preview.media_discovery:
            elements.append({"id": "a-home", "type": "link"})
        elif self.document is not None or manifest_path.is_file():
            manifest = json.loads(
                self.document if self.document is not None else manifest_path.read_bytes()
            )
            registered: set[str] = set()
            for slot, entry in manifest.get("slots", {}).items():
                # The template renders each slot in its own <section> as
                #   <picture data-media-id="studio-picture-<slot>">
                #     <source type=image/avif srcset="hero.avif?sha=…">
                #     <img data-media-id="studio-img-<slot>" src="hero.webp?sha=…"
                #          alt="<label>" loading="eager">
                #   </picture><p><label></p>
                # A browser fetches ONE file (avif when offered); the bridge
                # registers the <picture> and the <img>, each with the id
                # generateMediaId mints, and SKIPS a duplicate id.
                files, shas = entry.get("files", {}), entry.get("sha256", {})
                if not files:
                    continue
                fallback = next(iter(files))  # the <img src>
                fmt = "avif" if "avif" in files else fallback
                name = files[fmt]
                width = decoded_width(site_dir / name)
                sources = [
                    {
                        "srcset": f"/{self.site}/{files[f]}?sha={shas.get(f, '')}",
                        "type": f"image/{f}",
                    }
                    for f in files
                    if f != fallback
                ]
                img_meta = {
                    "src": f"/{self.site}/{name}?sha={shas.get(fmt, '')}",
                    "naturalWidth": width,
                    "loadingState": self.preview.loading_state(width),
                    "mediaType": "image",
                }
                picture_meta = {**img_meta, "mediaType": "picture", "sources": sources}
                explicit = self.preview.template_media_ids
                section = DomNode("section", children=["picture", "p"])
                picture = DomNode(
                    "picture",
                    {"data-media-id": f"studio-picture-{slot}"} if explicit else {},
                    parent=section,
                )
                img_attrs = {
                    "alt": entry.get("label", ""),
                    "src": f"{files[fallback]}?sha={shas.get(fallback, '')}",
                }
                if explicit:
                    img_attrs["data-media-id"] = f"studio-img-{slot}"
                img = DomNode(
                    "img", img_attrs, parent=DomNode("picture", children=["source", "img"])
                )
                for node, meta in ((picture, picture_meta), (img, img_meta)):
                    mid = generate_media_id(node)
                    if mid in registered:  # registerMediaElement: duplicate id -> skipped
                        continue
                    registered.add(mid)
                    elements.append({"id": mid, "category": "media", "mediaMetadata": meta})
        return {"page": {"url": self.url}, "elements": elements}

    def page_health(self) -> dict[str, object]:
        self.preview.probed.append(self.url or "")
        return {"success": True, "data": {"summary": self.preview.summary, "findings": []}}


class FakePreview:
    """A fake preview server + UI Bridge for the template sites. The D7 gates
    run for real against it: manifest sha check, navigation bound to the
    batch's preview URL, page-health summary, and image decoding."""

    def __init__(self, previews: Path, summary: str = "OK") -> None:
        self.previews = previews
        self.summary = summary
        self.probed: list[str] = []
        self.navigated: list[str] = []
        self.ignore_navigate = False
        self.stale_url: dict[str, str] = {}
        # Snapshots that report images still decoding before they settle.
        self.pending_snapshots = 0
        # The README site template gives every slot an explicit data-media-id;
        # False models a template without them (ids collide and are dropped).
        self.template_media_ids = True
        # False models an injected/seeded bridge that registers no media.
        self.media_discovery = True

    def loading_state(self, width: int) -> str:
        if self.pending_snapshots > 0:
            self.pending_snapshots -= 1
            return "pending"
        return "loaded" if width else "error"

    def fetch(self, url: str) -> bytes:
        """The preview server: serves any file of a site's preview dir."""
        site, _, name = urlsplit(url).path.lstrip("/").partition("/")
        return (self.previews / site / name).read_bytes()

    def bridge(self, base_url: str) -> FakeBridge:
        site = base_url.removeprefix("http://preview.test/").split("/", 1)[0]
        return FakeBridge(self, site)


@pytest.fixture
def preview(studio_root: Path) -> FakePreview:
    return FakePreview(studio_root.parent / "previews")


@pytest.fixture
def make_context(
    studio_root: Path, clock: FakeClock, preview: FakePreview
) -> Callable[..., Context]:
    def _make(**overrides: object) -> Context:
        ws = Workspace(studio_root)
        inventory = load_providers(ws.providers_path)
        ctx = Context(
            workspace=ws,
            conn=connect(ws.db_path),
            inventory=inventory,
            sites=load_sites(ws.sites_dir),
            adapters=_async_fakes(build_adapters(inventory)),
            notifier=FileNotifier(ws.notices_dir),
            clock=clock,
            sleep=clock.sleep,
            bridge=preview.bridge,
            fetch=preview.fetch,
        )
        for k, v in overrides.items():
            setattr(ctx, k, v)
        return ctx

    return _make


@pytest.fixture(autouse=True)
def _no_studio_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The studio host exports the hub clone/preview roots and the web tenant
    pin; tests never inherit them. A test that needs one sets it itself."""
    for name in ("STUDIO_HUB_CLONE", "STUDIO_HUB_PREVIEW", "STUDIO_WEB_ACTIVE_TENANT"):
        monkeypatch.delenv(name, raising=False)
