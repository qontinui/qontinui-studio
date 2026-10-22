"""Plan Phase 3 acceptance, offline: the launch kit runs whole on a fixture
build record and a local static "running product" served over HTTP, producing
the full bundle — pages, screenshots, walkthrough, social images, posts and a
short film (when ffmpeg is present) — and publishes it through the gates.

The LIVE self-launch additionally needs Phase 1 deployed (the public
build-record route answers 404 today) and the operator's public-org decision.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from qontinui_studio.adapters import build_adapters
from qontinui_studio.cli import main
from qontinui_studio.errors import ConfigError
from qontinui_studio.launch.kit import KitOptions, run_kit
from qontinui_studio.launch.product import load_product
from qontinui_studio.notify import FileNotifier
from qontinui_studio.pipeline import Context
from qontinui_studio.providers import load_providers
from qontinui_studio.sites import load_sites
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

from .imagehost import closed_port
from .launch_support import (
    document,
    launch_preview,
    playwright_ready,
    preview_ctx,
    preview_lines,
    public_client,
    use_public_door,
    write_launch_decor,
    write_product,
)

REPO = Path(__file__).resolve().parent.parent
FFMPEG = shutil.which("ffmpeg") is not None
_PW_SKIP = playwright_ready()
needs_playwright = pytest.mark.skipif(_PW_SKIP is not None, reason=_PW_SKIP or "")


@pytest.fixture(autouse=True)
def _public_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publication re-reads the factory's doors; here they are in-process fakes."""
    use_public_door(monkeypatch)


@pytest.fixture
def record_file(tmp_path: Path) -> Path:
    path = tmp_path / "record.json"
    path.write_text(json.dumps(document()))
    return path


@needs_playwright
def test_full_kit_on_a_served_static_site(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    write_launch_decor(studio_root)
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=True, brief=True, video_seconds=6)
    run = run_kit(ctx, product_path, KitOptions(video=FFMPEG), public_client(), publish=True)
    out = run.bundle.out_dir
    manifest = run.bundle.manifest

    # Real screenshots of the served product, at the script's viewport.
    shots = [i for i in manifest["images"] if i["kind"] == "screenshot"]
    assert [s["path"] for s in shots] == ["screenshots/home.png", "screenshots/detail.png"]
    assert all(s["ai_generated"] is False and "captured_from" not in s for s in shots)
    with Image.open(out / "screenshots" / "home.png") as img:
        assert img.size == (640, 360)
    # Playwright's own recording of the walkthrough.
    assert (out / "video" / "walkthrough.webm").stat().st_size > 0

    index = (out / "index.html").read_text()
    assert "screenshots/detail.png" in index and "(captured from the running product)" in index
    assert (out / "how-it-was-built.html").is_file()
    assert all(
        (out / "posts" / f"{n}.txt").is_file() for n in ("x", "bluesky", "mastodon", "linkedin")
    )

    if FFMPEG:
        film = manifest["film"]
        assert (
            film["path"] == "video/launch.mp4" and (out / "video" / "launch.mp4").stat().st_size > 0
        )
        assert abs(film["actual_seconds"] - 6) < 1.5
        names = [s["name"] for s in film["segments"]]
        assert names[:2] == ["title", "walkthrough"]
        assert names[-3:] == ["summary", "broll", "closing"]
        assert set(names[2:-3]) <= {"walkthrough-stills"}
        assert film["segments"][1]["source"].startswith("Playwright recording")
        # The replay slot is empty and says why — never faked.
        assert "sibling plan" in film["omitted"]["replay"]
        assert "video/launch.mp4" in index
    else:
        assert any("film not rendered" in n for n in run.bundle.notes)

    assert run.publish is not None and run.publish.published, run.publish.failures
    published = studio_root / "published-launch"
    assert (published / "index.html").is_file() and (
        published / "screenshots" / "home.png"
    ).is_file()


@needs_playwright
@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_walkthrough_from_stills_when_recording_is_off(
    make_context: Callable[..., Context], studio_root: Path, record_file: Path
) -> None:
    ctx = make_context(**preview_ctx(studio_root))
    product_path = write_product(studio_root, capture=True, video_seconds=5)
    product_path.write_text(
        product_path.read_text().replace("record_video: true", "record_video: false")
    )
    run = run_kit(ctx, product_path, KitOptions(), public_client())
    film = run.bundle.manifest["film"]
    walk = next(s for s in film["segments"] if s["name"] == "walkthrough")
    assert walk["source"] == "screenshots of the running product stitched by ffmpeg"
    assert "broll" in film["omitted"]


@needs_playwright
@pytest.mark.local_http
def test_self_launch_with_the_shipped_product_definition(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kit launches ITSELF: the repo's own products/qontinui-studio.yaml,
    capturing the studio's own UI (its review page), on a fixture record, into
    the hub clone the yaml pins with ${STUDIO_HUB_CLONE}."""
    # The shipped local-gpu row points at a loopback facade; here none listens
    # (a port nothing is bound to), so the local GPU answers a typed busy.
    monkeypatch.setenv("STUDIO_LOCAL_GPU_BASE_URL", f"http://127.0.0.1:{closed_port()}/v1")
    root = tmp_path / "self"
    # The hub clone and the hub preview live OUTSIDE the studio root (C1/AG2).
    hub_clone = tmp_path / "hub-publish"
    hub_preview = tmp_path / "hub-preview"
    monkeypatch.setenv("STUDIO_HUB_CLONE", str(hub_clone))
    monkeypatch.setenv("STUDIO_HUB_PREVIEW", str(hub_preview))
    for name in ("products", "sites", "briefs", "style"):
        shutil.copytree(REPO / name, root / name)
    shutil.copy(REPO / "providers.yaml", root / "providers.yaml")
    product_yaml = root / "products" / "qontinui-studio.yaml"
    text = product_yaml.read_text().replace("video_seconds: 60", "video_seconds: 5")
    # The shipped yaml declares the hub preview (127.0.0.1:4330, `hub preview`);
    # here the fake preview server + UI Bridge stand in for it: its four D7
    # fields REPLACE the shipped ones.
    shipped = [
        line
        for line in text.splitlines()
        if line.strip().startswith(
            ("ui_bridge_url:", "preview_url:", "preview_dir:", "preview_manifest_url:")
        )
    ]
    assert len(shipped) == 4, shipped
    text = text.replace(shipped[0], "\n".join(preview_lines("launch", "  ")))
    for line in shipped[1:]:
        text = text.replace(line + "\n", "")
    product_yaml.write_text(text)
    product = load_product(product_yaml)
    assert product.publish.publish_dir == (hub_clone / "public" / "qontinui-studio").resolve()
    assert product.publish.preview_url == "http://preview.test/launch/index.html"

    ws = Workspace(root)
    inventory = load_providers(ws.providers_path)
    fake = launch_preview(root)
    ctx = Context(
        workspace=ws,
        conn=connect(ws.db_path),
        inventory=inventory,
        sites=load_sites(ws.sites_dir),
        adapters=build_adapters(inventory),
        notifier=FileNotifier(ws.notices_dir),
        bridge=fake.bridge,
        fetch=fake.fetch,
    )
    run = run_kit(ctx, product_yaml, KitOptions(video=FFMPEG), public_client(), publish=True)
    # The local GPU is the only schedulable generator and no facade is
    # listening: the decoration batch waits on a typed busy, and the launch
    # still assembles and publishes without it (D1).
    assert any("qontinui-studio-launch" in n for n in run.bundle.notes), run.bundle.notes
    assert run.publish is not None and run.publish.published, run.publish.failures
    published = hub_clone / "public" / "qontinui-studio"
    assert (published / "screenshots" / "review.png").is_file()
    assert not (root / ".studio" / "published").exists()
    page = (published / "how-it-was-built.html").read_text()
    # The footer links the record page beside the bundle on the hub
    # (record.public_url), not the API route.
    footer = "https://build.qontinui.io/qontinui-studio/how-it-was-built.html"
    assert footer in page and footer in (published / "index.html").read_text()
    # The hub's UI Bridge boot script loads on every bundle page.
    assert "/_hub/ui-bridge.js" in (published / "index.html").read_text()
    # The published notice names the hub's one-step retraction.
    notice = next(ws.notices_dir.glob(f"{run.launch_id}-published-*.md")).read_text()
    assert "hub unpublish qontinui-studio" in notice
    # ...and the public pages on the hub (publish.public_url), not the clone.
    assert "https://build.qontinui.io/qontinui-studio/index.html" in notice
    assert str(published / "index.html") not in notice
    # og:image is absolute, under the bundle's public URL.
    assert (
        "content='https://build.qontinui.io/qontinui-studio/social/"
        in (published / "index.html").read_text()
    )
    if FFMPEG:
        assert (published / "video" / "launch.mp4").is_file()


def test_shipped_product_definition_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_HUB_CLONE", str(tmp_path / "hub-publish"))
    monkeypatch.setenv("STUDIO_HUB_PREVIEW", str(tmp_path / "hub-preview"))
    product = load_product(REPO / "products" / "qontinui-studio.yaml")
    assert product.publish.publish_dir == (tmp_path / "hub-publish" / "public" / "qontinui-studio")
    assert product.publish.preview_dir == (tmp_path / "hub-preview" / "qontinui-studio")
    assert product.publish.has_preview and product.publish.page_scripts == ("/_hub/ui-bridge.js",)
    assert product.publish.unpublish_command == "hub unpublish qontinui-studio"
    assert product.publish.public_url == "https://build.qontinui.io/qontinui-studio/"
    assert product.record.door == "public"
    assert product.slug == "qontinui-studio" and product.repos == ["qontinui/qontinui-studio"]
    assert product.capture is not None and product.capture.script.is_file()
    assert product.brief is not None and product.brief.is_file()
    r = CliRunner().invoke(main, ["--root", str(REPO), "brief", "validate", str(product.brief)])
    # The brief is well-formed and runnable: its first preference is the
    # unmetered local GPU.
    assert r.exit_code == 0, r.output
    assert r.output.strip().endswith(": ok"), r.output


def test_shipped_product_definition_names_an_unset_hub_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the studio host's hub clone, the shipped yaml is refused at
    load, naming the variable — it never resolves into whatever checkout holds
    the yaml (C1)."""
    monkeypatch.delenv("STUDIO_HUB_CLONE", raising=False)
    monkeypatch.setenv("STUDIO_HUB_PREVIEW", str(tmp_path / "hub-preview"))
    for name in ("qontinui-studio", "qontinui-factory-hub"):
        with pytest.raises(ConfigError, match="STUDIO_HUB_CLONE is not set"):
            load_product(REPO / "products" / f"{name}.yaml")


def test_shipped_hub_product_definition_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_HUB_CLONE", str(tmp_path / "hub-publish"))
    monkeypatch.setenv("STUDIO_HUB_PREVIEW", str(tmp_path / "hub-preview"))
    product = load_product(REPO / "products" / "qontinui-factory-hub.yaml")
    assert product.slug == "qontinui-factory-hub" and product.repos == ["qontinui-factory/hub"]
    assert product.homepage == "https://build.qontinui.io/"
    assert product.record.door == "public"
    assert product.record_url() == (
        "https://build.qontinui.io/qontinui-factory-hub/how-it-was-built.html"
    )
    assert product.publish.publish_dir == (
        tmp_path / "hub-publish" / "public" / "qontinui-factory-hub"
    )
    assert product.publish.preview_dir == (tmp_path / "hub-preview" / "qontinui-factory-hub")
    assert product.publish.has_preview
    assert product.publish.preview_url == "http://127.0.0.1:4330/qontinui-factory-hub/"
    assert product.publish.unpublish_command == "hub unpublish qontinui-factory-hub"
    assert product.publish.public_url == "https://build.qontinui.io/qontinui-factory-hub/"
    assert product.capture is None and product.brief is None


@pytest.mark.parametrize("slug", ["qontinui-studio", "qontinui-factory-hub"])
def test_shipped_hub_product_serves_its_footer_page_beside_the_bundle(
    slug: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub product's bundle is served at build.qontinui.io/<slug>/ and its
    footer page sits beside it: without publish.public_url the published
    notice would name files in the hub clone and og:image would be relative."""
    monkeypatch.setenv("STUDIO_HUB_CLONE", str(tmp_path / "hub-publish"))
    monkeypatch.setenv("STUDIO_HUB_PREVIEW", str(tmp_path / "hub-preview"))
    product = load_product(REPO / "products" / f"{slug}.yaml")
    base = f"https://build.qontinui.io/{slug}/"
    assert product.publish.public_url == base
    assert product.record_url() == base + "how-it-was-built.html"


def test_cli_build_exits_4_without_a_record(
    studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    import respx

    monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)
    product_path = write_product(studio_root, capture=False)
    with respx.mock:
        respx.get("https://api.qontinui.io/api/v1/public/build-records/qontinui-studio").mock(
            return_value=httpx.Response(404, json={"detail": "Not Found"})
        )
        r = CliRunner().invoke(
            main, ["--root", str(studio_root), "launch", "build", str(product_path)]
        )
    assert r.exit_code == 4, r.output
    assert "record: NONE (not_found)" in r.output


@needs_playwright
def test_cli_launch_capture(studio_root: Path, tmp_path: Path) -> None:
    product_path = write_product(studio_root, capture=True)
    out = tmp_path / "shots"
    r = CliRunner().invoke(
        main,
        ["--root", str(studio_root), "launch", "capture", str(product_path), "--out", str(out)],
    )
    assert r.exit_code == 0, r.output
    assert (out / "home.png").is_file() and (out / "detail.png").is_file()
    assert "video:" in r.output
