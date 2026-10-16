"""Gateway tenants (plan 2026-10-13 Phase 11 step 3): image calls go through a
declared gateway's images route, or are refused with the typed
``gateway_has_no_image_route`` before any vendor or local call."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio import cli
from qontinui_studio.adapters import AdapterSet, build_adapters
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.adapters.openai_compatible_images import OpenAICompatibleImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.errors import GatewayRefusal
from qontinui_studio.gateway import GatewayState, read_gateway_state
from qontinui_studio.pipeline import Context, new_batch, run_brief
from qontinui_studio.providers import load_providers

from . import imagehost
from .imagehost import ImageHost

LOCAL_ROW = """
  - id: local-gpu
    provider: Local image facade
    plan: self-hosted
    api_available: {value: "yes", source: "https://local.test/api"}
    automation_allowed: {value: "yes", source: "https://local.test/api"}
    commercial_use: {value: "yes", source: "https://local.test/licence"}
    watermark: IPTC trainedAlgorithmicMedia (XMP)
    adapter: openai_compatible_images
    model: z-image-turbo
    base_url: http://127.0.0.1:8100/v1
    capacity: unmetered
    tos_reviewed: 2026-10-15
    domains: [local.test]
"""


@pytest.fixture
def host() -> Iterator[ImageHost]:
    h = imagehost.start(ImageHost())
    yield h
    imagehost.stop(h)


def _runner(host: ImageHost) -> str:
    return host.v1_url.removesuffix("/v1")


@pytest.mark.local_http
@pytest.mark.parametrize(
    ("reply", "state", "source", "images"),
    [
        (imagehost.json_reply(404, {"error": "not found"}), "not_declared", "route_absent", None),
        (
            imagehost.json_reply(200, {"success": True, "data": {"state": "not_declared"}}),
            "not_declared",
            "runner",
            None,
        ),
        (
            imagehost.json_reply(200, {"state": "declared", "reason": "anthropic messages only"}),
            "declared",
            "runner",
            None,
        ),
        (
            imagehost.json_reply(
                200,
                {"data": {"state": "declared", "routes": {"images": "http://10.0.0.5:9000/v1"}}},
            ),
            "declared",
            "runner",
            "http://10.0.0.5:9000/v1",
        ),
        (imagehost.json_reply(200, {"state": "unknown", "reason": "x"}), "unknown", "runner", None),
        (imagehost.json_reply(500, {}), "unknown", "unreadable", None),
        (imagehost.raw_reply(200, b"<html>"), "unknown", "unreadable", None),
        (imagehost.json_reply(200, {"state": "maybe"}), "unknown", "unreadable", None),
    ],
    ids=[
        "route-absent",
        "not-declared",
        "declared-no-images",
        "declared-images",
        "unknown",
        "5xx",
        "unparseable",
        "bad-state",
    ],
)
def test_the_runner_reading_maps_to_a_typed_state(
    host: ImageHost, reply: imagehost.Reply, state: str, source: str, images: str | None
) -> None:
    host.queue(imagehost.runner_health(), reply)
    got = read_gateway_state(_runner(host))
    assert (got.state, got.source, got.images_base_url) == (state, source, images)
    assert [r.path for r in host.received] == ["/health", "/model-gateway"]
    assert got.runner_git_sha == imagehost.RUNNER_GIT_SHA


@pytest.mark.local_http
def test_no_runner_on_the_box_means_no_declared_gateway() -> None:
    got = read_gateway_state(f"http://127.0.0.1:{imagehost.closed_port()}")
    assert got.state == "not_declared" and got.source == "no_runner"
    assert got.refusal() is None


@pytest.mark.parametrize(
    ("state", "images", "refused"),
    [
        ("not_declared", None, False),
        ("declared", None, True),
        ("declared", "http://127.0.0.1:9000/v1", False),
        ("unknown", None, True),
        ("unknown", "http://127.0.0.1:9000/v1", True),
    ],
)
def test_which_states_refuse(state: str, images: str | None, refused: bool) -> None:
    refusal = GatewayState(state, "runner", "r", images).refusal()  # type: ignore[arg-type]
    assert (refusal is not None) is refused
    if refusal is not None:
        assert refusal.code == "gateway_has_no_image_route" and refusal.state == state


def test_a_refusing_gateway_builds_no_adapter(studio_root: Path) -> None:
    inv = load_providers(studio_root / "providers.yaml")
    built = build_adapters(inv, gateway=GatewayState("declared", "runner"))
    assert built.adapters == {} and built.refusal is not None
    assert all("gateway_has_no_image_route" in why for why in built.unavailable.values())


def test_a_declared_images_route_carries_every_openai_wire_adapter(studio_root: Path) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + LOCAL_ROW)
    route = "http://10.1.2.3:9000/v1"
    built = build_adapters(load_providers(p), gateway=GatewayState("declared", "runner", "", route))
    local = built.adapters["local-gpu"]
    assert isinstance(local, OpenAICompatibleImageAdapter) and local.base_url == route
    # The fakes speak no OpenAI images wire, so they cannot go through it.
    assert "fake" not in built.adapters and "OpenAI images wire" in built.unavailable["fake"]


def test_unmetered_capacity_cannot_ride_a_remote_gateway_route(studio_root: Path) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + LOCAL_ROW)
    built = build_adapters(
        load_providers(p),
        gateway=GatewayState("declared", "runner", "", "https://gateway.example.com/v1"),
    )
    assert "local-gpu" not in built.adapters
    assert "not on the local network" in built.unavailable["local-gpu"]


def test_pipeline_refuses_before_any_provider_call(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    fake = FakeImageAdapter("fake")
    refusal = GatewayState("unknown", "unreadable", "HTTP 500").refusal()
    ctx = make_context(adapters=AdapterSet(adapters={"fake": fake}, refusal=refusal))
    path = write_brief()
    with pytest.raises(GatewayRefusal):
        run_brief(ctx, path)
    assert ctx.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    # A batch made before the refusal is not driven either.
    ctx.adapters.refusal = None
    batch = new_batch(ctx, load_brief(path))
    ctx.adapters.refusal = refusal
    from qontinui_studio.pipeline import drive_batch

    with pytest.raises(GatewayRefusal):
        drive_batch(ctx, batch)
    assert fake.submitted == []


@pytest.mark.parametrize("command", [["brief", "run"], ["jobs", "run"]])
def test_cli_exits_6_with_the_typed_refusal(
    studio_root: Path,
    write_brief: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    monkeypatch.setattr(
        cli, "read_gateway_state", lambda: GatewayState("declared", "runner", "messages only")
    )
    args = ["--root", str(studio_root), *command]
    if command[0] == "brief":
        args.append(str(write_brief()))
    r = CliRunner().invoke(cli.main, args)
    assert r.exit_code == 6, r.output
    assert "refused: gateway_has_no_image_route (gateway state: declared)" in r.output
    db = studio_root / ".studio" / "studio.db"
    if db.exists():
        import sqlite3

        assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
