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
from qontinui_studio.launch.kit import KitOptions, run_kit
from qontinui_studio.launch.product import load_product
from qontinui_studio.notify import FileNotifier
from qontinui_studio.pipeline import Context
from qontinui_studio.providers import load_providers
from qontinui_studio.sites import load_sites
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

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
def test_self_launch_with_the_shipped_product_definition(tmp_path: Path, record_file: Path) -> None:
    """The kit launches ITSELF: the repo's own products/qontinui-studio.yaml,
    capturing the studio's own UI (its review page), on a fixture record."""
    root = tmp_path / "self"
    for name in ("products", "sites", "briefs", "style"):
        shutil.copytree(REPO / name, root / name)
    shutil.copy(REPO / "providers.yaml", root / "providers.yaml")
    product_yaml = root / "products" / "qontinui-studio.yaml"
    product_yaml.write_text(
        product_yaml.read_text().replace("video_seconds: 60", "video_seconds: 5")
    )
    args = [
        "--root",
        str(root),
        "launch",
        "run",
        str(product_yaml),
        "--record-file",
        str(record_file),
    ]
    if not FFMPEG:
        args.append("--no-video")
    r = CliRunner().invoke(main, args)
    # The shipped definition declares no page-health URL (where the bundle is
    # hosted is the operator's open decision), and a launch is a product, so
    # page health cannot be waived: the gates REFUSE, nothing is copied.
    assert r.exit_code == 2, r.output
    assert "not_configured  d7_page_health" in r.output
    # No generator has a quota in the shipped inventory: decoration is skipped
    # with a note, and the launch still assembles (D1).
    assert "decorative assets not generated" in r.output
    published = root / ".studio" / "published" / "qontinui-studio"
    assert not (published / "index.html").exists()  # only the publish lock was taken

    # With a D7 preview declared (a fake preview server + UI Bridge), the same
    # shipped definition publishes its own launch.
    product_yaml.write_text(
        product_yaml.read_text().replace(
            "  commercial: false",
            "  commercial: false\n" + "\n".join(preview_lines("launch", "  ")),
        )
    )
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
    assert run.publish is not None and run.publish.published, run.publish.failures
    assert (published / "screenshots" / "review.png").is_file()
    page = (published / "how-it-was-built.html").read_text()
    assert "https://api.qontinui.io/api/v1/public/build-records/qontinui-studio" in page
    if FFMPEG:
        assert (published / "video" / "launch.mp4").is_file()


def test_shipped_product_definition_is_valid() -> None:
    product = load_product(REPO / "products" / "qontinui-studio.yaml")
    assert product.slug == "qontinui-studio" and product.repos == ["qontinui/qontinui-studio"]
    assert product.capture is not None and product.capture.script.is_file()
    assert product.brief is not None and product.brief.is_file()
    r = CliRunner().invoke(main, ["--root", str(REPO), "brief", "validate", str(product.brief)])
    # The brief itself is well-formed; the only problem allowed is that no
    # provider has a quota yet (the operator's paid-API decision).
    problems = [ln for ln in r.output.splitlines() if ln.strip()]
    assert all("no preferred provider is schedulable" in ln for ln in problems), problems


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
