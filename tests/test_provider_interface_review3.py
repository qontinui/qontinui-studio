"""Regressions for the third independent review of plan 2026-10-13 Phase 11 at
bce8c2b0: F1 (bulk spill across runs), F2 (the gateway picks the bearer
variable), the ce25cdab gateway-read hardening, L-a (XMP parsed as XML), L-c
(one packet) and L-d (the adapter backfill does not depend on command order)."""

from __future__ import annotations

import io
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image, PngImagePlugin

from qontinui_studio import marker
from qontinui_studio.adapters import AdapterSet, GenerationRequest, build_adapter, build_adapters
from qontinui_studio.briefs import load_brief
from qontinui_studio.errors import AdapterDisabledError, ProviderBusy, ProviderDeferred
from qontinui_studio.gateway import GatewayState, read_gateway_state
from qontinui_studio.pipeline import Context, _request_for, new_batch
from qontinui_studio.providers import load_providers
from qontinui_studio.store import connect

from . import imagehost
from .conftest import FakeClock
from .imagehost import ImageHost
from .test_provider_interface_review import (
    LOCAL_ROW,
    OPENAI_ROW,
    Scripted,
    _v1_db_with_inflight_jobs,
    _with,
)


@pytest.fixture
def host() -> Iterator[ImageHost]:
    h = imagehost.start(ImageHost())
    yield h
    imagehost.stop(h)


# ---- F1: a bulk job that was on the local GPU never reaches a paid adapter -------


@pytest.mark.parametrize(
    "refusal",
    [ProviderBusy("busy", 60.0), ProviderDeferred("deferred: outside the bulk window", 60.0)],
    ids=["busy", "deferred"],
)
def test_f1_a_bulk_job_refused_by_local_never_moves_to_paid_on_a_later_run(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
    refusal: Exception,
) -> None:
    _with(studio_root, LOCAL_ROW)
    path = write_brief(providers="local-gpu, fake")
    path.write_text(path.read_text() + "priority: bulk\n")
    local = Scripted("local-gpu", refusal)
    ctx = make_context(adapters=AdapterSet(adapters={"local-gpu": local, "fake": Scripted("fake")}))
    batch = new_batch(ctx, load_brief(path))
    req = {batch: _request_for(load_brief(path))}
    ctx.scheduler().step(req)
    row = ctx.conn.execute("SELECT provider, tried_providers, home_provider FROM jobs").fetchone()
    assert (row["provider"], row["tried_providers"], row["home_provider"]) == (
        "local-gpu",
        "[]",
        "local-gpu",
    )
    # A later run: the local adapter is not built (key unset, D4 off, a gateway).
    paid = Scripted("fake")
    ctx.adapters = AdapterSet(adapters={"fake": paid})
    clock.sleep(120)
    ctx.scheduler().step(req)
    assert paid.calls == 0
    row = ctx.conn.execute("SELECT status, last_error FROM jobs").fetchone()
    assert row["status"] == "failed" and row["last_error"].startswith("bulk_no_spill:")
    assert "home local-gpu has no adapter on this run" in row["last_error"]


def test_f1_a_bulk_job_whose_quota_ran_out_waits_for_its_home_not_another_paid_row(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
) -> None:
    second = (
        OPENAI_ROW.replace("id: openai", "id: openai2")
        .replace("openai.test", "openai2.test")
        .replace("provider: OpenAI test row", "provider: Second paid row")
        .replace("adapter: openai_images", "adapter: fake")
        .replace("    api_key_env: OPENAI_API_KEY\n", "")
    )
    _with(studio_root, second)
    path = write_brief(providers="fake, openai2")
    path.write_text(path.read_text() + "priority: bulk\n")
    from qontinui_studio.errors import QuotaExhaustedError

    first = Scripted("fake", QuotaExhaustedError("quota"))
    other = Scripted("openai2")
    ctx = make_context(adapters=AdapterSet(adapters={"fake": first, "openai2": other}))
    batch = new_batch(ctx, load_brief(path))
    ctx.scheduler().step({batch: _request_for(load_brief(path))})
    row = ctx.conn.execute("SELECT status, provider, home_provider FROM jobs").fetchone()
    assert other.calls == 0
    assert (row["status"], row["provider"], row["home_provider"]) == ("queued", None, "fake")


def test_f1_the_v2_migration_backfills_the_home_of_inflight_jobs(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    db, _ = _v1_db_with_inflight_jobs(make_context, write_brief, clock)
    raw = sqlite3.connect(db)
    raw.execute("UPDATE jobs SET tried_providers = '[\"gone\"]' WHERE variant_index = 0")
    raw.commit()
    raw.close()
    conn = connect(db)
    homes = [r[0] for r in conn.execute("SELECT home_provider FROM jobs ORDER BY variant_index")]
    assert homes == ["gone", "fake"]


# ---- F2: the gateway may not nominate an arbitrary bearer variable ---------------


@pytest.mark.local_http
@pytest.mark.parametrize(
    "name", ["OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "HOME", "qontinui_gateway_x"]
)
def test_f2_a_gateway_naming_a_foreign_variable_refuses_the_read(
    host: ImageHost, name: str
) -> None:
    host.queue(
        imagehost.runner_health(),
        imagehost.json_reply(
            200,
            {
                "state": "declared",
                "routes": {"images": "https://gw.example.com/v1", "images_credential_env": name},
            },
        ),
    )
    got = read_gateway_state(host.v1_url.removesuffix("/v1"))
    assert (got.state, got.source) == ("unknown", "unacceptable_credential_env")
    assert got.images_credential_env is None and got.refusal() is not None


@respx.mock
@pytest.mark.parametrize(
    "name", ["OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "SOME_VAR", "QONTINUI_GATEWAY_FACADE"]
)
def test_f2_no_adapter_is_built_and_no_request_made_with_a_foreign_variable(
    studio_root: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # The last name passes the prefix but IS an inventory row's api_key_env.
    row = LOCAL_ROW.replace("STUDIO_TEST_FACADE_KEY", "QONTINUI_GATEWAY_FACADE")
    inv = load_providers(_with(studio_root, row, OPENAI_ROW))
    monkeypatch.setenv(name, "secret-value")
    route = "https://10.1.2.3/v1"
    calls = respx.post(f"{route}/images/generations").mock(return_value=httpx.Response(500))
    built = build_adapters(inv, gateway=GatewayState("declared", "runner", "", route, name))
    assert built.adapters == {} and built.refusal is not None
    assert all("gateway_has_no_image_route" in why for why in built.unavailable.values())
    # The direct constructor refuses the same names (a row's own key, or no prefix).
    openai = inv.get("openai")
    with pytest.raises(AdapterDisabledError):
        build_adapter(openai, gateway_route=route, gateway_credential_env=name.replace(
            "QONTINUI_GATEWAY_FACADE", "OPENAI_API_KEY"))  # fmt: skip
    assert calls.call_count == 0


@respx.mock
def test_f2_a_gateway_prefixed_variable_is_the_one_sent(
    studio_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inv = load_providers(_with(studio_root, LOCAL_ROW, OPENAI_ROW))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-vendor")
    monkeypatch.setenv("QONTINUI_GATEWAY_IMAGES", "gw-secret")
    route = "https://10.1.2.3/v1"
    respx.post(f"{route}/images/generations").mock(
        return_value=httpx.Response(400, json={"error": {"message": "x"}})
    )
    built = build_adapters(
        inv, gateway=GatewayState("declared", "runner", "", route, "QONTINUI_GATEWAY_IMAGES")
    )
    with pytest.raises(Exception):  # noqa: B017 - the 400 itself is not the point
        built.adapters["openai"].submit(GenerationRequest("j", "p", "1:1"), tmp_path)
    assert respx.calls.last.request.headers["authorization"] == "Bearer gw-secret"


# ---- ce25cdab: what a 404, a timeout and a refusal each mean ---------------------


@pytest.mark.local_http
def test_a_404_from_an_identified_runner_is_not_declared(host: ImageHost) -> None:
    host.queue(imagehost.runner_health(), imagehost.json_reply(404, {"error": "not found"}))
    got = read_gateway_state(host.v1_url.removesuffix("/v1"))
    assert (got.state, got.source, got.runner_git_sha) == (
        "not_declared",
        "route_absent",
        imagehost.RUNNER_GIT_SHA,
    )


@pytest.mark.local_http
@pytest.mark.parametrize(
    "health",
    [
        imagehost.json_reply(404, {}),
        imagehost.json_reply(200, {"status": "ok"}),
        imagehost.json_reply(200, {"data": {"gitSha": "not-a-sha", "buildId": "b"}}),
        imagehost.json_reply(200, {"data": {"gitSha": "200538ff8d9b"}}),
        imagehost.raw_reply(200, b"<html>ok</html>", "text/html"),
    ],
    ids=["health-404", "no-identity", "bad-sha", "no-build-id", "html"],
)
def test_any_answer_from_an_unidentified_process_is_unknown(
    host: ImageHost, health: imagehost.Reply
) -> None:
    host.queue(health, imagehost.json_reply(404, {}))
    got = read_gateway_state(host.v1_url.removesuffix("/v1"))
    assert (got.state, got.source) == ("unknown", "unidentified_runner")
    assert got.refusal() is not None
    assert [r.path for r in host.received] == ["/health"]


@pytest.mark.local_http
def test_a_declared_answer_from_an_unidentified_process_is_unknown(host: ImageHost) -> None:
    host.queue(
        imagehost.json_reply(200, {"status": "ok"}),
        imagehost.json_reply(200, {"state": "not_declared"}),
    )
    got = read_gateway_state(host.v1_url.removesuffix("/v1"))
    assert got.state == "unknown" and got.refusal() is not None


def _client(exc: type[httpx.TransportError]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("simulated", request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("exc", "state", "source"),
    [
        (httpx.ConnectTimeout, "unknown", "runner_unresponsive"),
        (httpx.ConnectError, "not_declared", "no_runner"),
        (httpx.ReadTimeout, "unknown", "unreadable"),
    ],
    ids=["connect-timeout", "connect-refused", "read-timeout"],
)
def test_only_a_refused_connection_means_no_runner(
    exc: type[httpx.TransportError], state: str, source: str
) -> None:
    got = read_gateway_state("http://127.0.0.1:9876", http=_client(exc))
    assert (got.state, got.source) == (state, source)
    assert (got.refusal() is None) is (state == "not_declared")


@pytest.mark.local_http
def test_no_runner_at_all_still_means_no_declared_gateway() -> None:
    got = read_gateway_state(f"http://127.0.0.1:{imagehost.closed_port()}")
    assert (got.state, got.source) == ("not_declared", "no_runner")


# ---- L-a: XMP is parsed as XML ---------------------------------------------------

IPTC = marker.IPTC_EXT_NS
RDF = marker.RDF_NS
DST = marker.DIGITAL_SOURCE_TYPE


def _packet(body: str, iptc_prefix: str = "Iptc4xmpExt", iptc_uri: str = IPTC) -> bytes:
    return (
        f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="{RDF}" '
        f'xmlns:{iptc_prefix}="{iptc_uri}">{body}</rdf:RDF></x:xmpmeta>'
    ).encode()


def _png(packet: bytes) -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", packet.decode(), zip=False)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def _values(data: bytes) -> list[str]:
    packets = marker._packets(data)
    assert packets is not None and len(packets) == 1
    root = marker._parse_packet(packets[0])
    assert root is not None
    return marker._dst_values(root)


def test_la_a_digital_source_type_inside_a_comment_does_not_count() -> None:
    body = f'<!-- <rdf:Description Iptc4xmpExt:DigitalSourceType="{DST}"/> -->'
    body += '<rdf:Description rdf:about=""/>'
    data = _png(_packet(body))
    assert DST.encode() in data
    assert not marker.bytes_are_marked(data)
    marked = marker.mark_bytes(data)
    assert marker.bytes_are_marked(marked) and _values(marked) == [DST]


def test_la_the_property_is_matched_by_namespace_uri_not_prefix() -> None:
    attr = f'<rdf:Description rdf:about="" {{p}}:DigitalSourceType="{DST}"/>'
    other_prefix = _png(_packet(attr.format(p="iptc"), iptc_prefix="iptc"))
    assert marker.bytes_are_marked(other_prefix)
    wrong_uri = _png(_packet(attr.format(p="Iptc4xmpExt"), iptc_uri="http://example.com/fake/"))
    assert not marker.bytes_are_marked(wrong_uri)
    marked = marker.mark_bytes(wrong_uri)
    assert marker.bytes_are_marked(marked) and _values(marked) == [DST]


@pytest.mark.parametrize(
    "body",
    [
        '<rdf:Description rdf:about="" Iptc4xmpExt:DigitalSourceType='
        '"http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"/>',
        '<rdf:Description rdf:about=""><Iptc4xmpExt:DigitalSourceType rdf:resource='
        '"http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"/></rdf:Description>',
        '<rdf:Description rdf:about=""><Iptc4xmpExt:DigitalSourceType>'
        "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"
        "</Iptc4xmpExt:DigitalSourceType></rdf:Description>"
        f'<rdf:Description rdf:about="" Iptc4xmpExt:DigitalSourceType="{DST}"/>',
    ],
    ids=["attribute", "resource-element", "conflicting-pair"],
)
def test_la_an_existing_value_is_replaced_never_kept_beside_the_marker(body: str) -> None:
    data = _png(_packet(body))
    assert not marker.bytes_are_marked(data)
    marked = marker.mark_bytes(data)
    assert marker.bytes_are_marked(marked)
    assert set(_values(marked)) == {DST}
    assert b"digitalCapture" not in marked


def test_la_a_packet_declaring_entities_is_refused_never_expanded() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    ) + _packet(f'<rdf:Description Iptc4xmpExt:DigitalSourceType="{DST}">&b;</rdf:Description>')
    assert marker._parse_packet(bomb) is None
    data = _png(bomb)
    assert not marker.bytes_are_marked(data)
    marked = marker.mark_bytes(data)
    assert marker.bytes_are_marked(marked) and b"ENTITY" not in marked


# ---- L-c: exactly one packet, whichever side the extra one is on -----------------


def test_lc_an_extra_packet_after_the_marked_one_is_not_marked() -> None:
    marked = marker.mark_bytes(_png(_packet('<rdf:Description rdf:about=""/>')))
    chunks = marker._png_chunks(marked)
    xmp_end = next(b for _a, b, kind, _p in chunks if kind == b"iTXt")
    other = _packet('<rdf:Description rdf:about=""/>')
    data = marked[:xmp_end] + marker._png_itxt(other) + marked[xmp_end:]
    assert len(marker._packets(data) or []) == 2
    assert not marker.bytes_are_marked(data)
    assert marker.bytes_are_marked(marker.mark_bytes(data))


# ---- L-d: the adapter backfill does not depend on which command runs first ------


@pytest.mark.parametrize("inventory_first", [True, False], ids=["inventory-first", "plain-first"])
def test_ld_the_backfill_happens_whichever_connect_runs_first(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
    inventory_first: bool,
) -> None:
    db, _ = _v1_db_with_inflight_jobs(make_context, write_brief, clock)
    kinds = load_providers(studio_root / "providers.yaml").adapter_kinds()
    order: list[dict[str, str] | None] = [kinds, None] if inventory_first else [None, kinds]
    for adapters in order:
        connect(db, adapters).close()
    conn = connect(db)
    assert [r[0] for r in conn.execute("SELECT adapter FROM jobs")] == ["fake", "fake"]


# The signature stays on one line: the arming prover reads a test's region from
# its def line, and a multi-line signature's closing paren ends that region.
def test_ld_a_library_command_first_then_a_run_command_backfills(studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock) -> None:  # fmt: skip
    from qontinui_studio.cli import main

    db, _ = _v1_db_with_inflight_jobs(make_context, write_brief, clock)
    # ``main`` in standalone mode, exactly as the ``studio`` console script runs it.
    with pytest.raises(SystemExit) as listed:
        main(["--root", str(studio_root), "library", "list"])
    assert listed.value.code == 0
    raw = sqlite3.Connection(db)
    assert [x[0] for x in raw.execute("SELECT adapter FROM jobs")] == [None, None]
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
    raw.close()
    # The run goes on to the publish gates, which are not the subject here: only
    # that it ran to a typed exit and backfilled on its own open of the file.
    with pytest.raises(SystemExit):
        main(["--root", str(studio_root), "jobs", "run"])
    raw = sqlite3.Connection(db)
    adapters = {x[0] for x in raw.execute("SELECT adapter FROM jobs")}
    raw.close()
    assert adapters == {"fake"}
