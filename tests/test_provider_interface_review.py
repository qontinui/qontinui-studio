"""Regressions for the independent review of plan 2026-10-13 Phase 11 at
f9212e6f (findings M1-M4, L1, Low 1-7). Each test names the finding it pins."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner
from PIL import Image, PngImagePlugin

from qontinui_studio import cli, marker, pipeline, provenance
from qontinui_studio.adapters import AdapterSet, GenerationRequest, SubmittedJob, build_adapters
from qontinui_studio.adapters.base import write_staged
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.adapters.openai_compatible_images import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    OpenAICompatibleImageAdapter,
)
from qontinui_studio.adapters.openai_images import OpenAIImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.errors import (
    MAX_RETRY_AFTER_S,
    PermanentProviderError,
    ProviderBusy,
    ProviderDisabled,
    ProviderOutcomeUnknown,
)
from qontinui_studio.gateway import GatewayState, read_gateway_state
from qontinui_studio.pipeline import Context, _request_for, drive_batch, new_batch
from qontinui_studio.providers import load_providers
from qontinui_studio.store import connect

from . import imagehost
from .conftest import FakeClock
from .imagehost import ImageHost
from .launch_support import write_product

OTHER_XMP = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" dc:creator="provider-x"/></rdf:RDF></x:xmpmeta>'
)

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
    api_key_env: STUDIO_TEST_FACADE_KEY
    capacity: unmetered
    tos_reviewed: 2026-10-15
    domains: [local.test]
"""

OPENAI_ROW = """
  - id: openai
    provider: OpenAI test row
    plan: test
    api_available: {value: "yes", source: "https://openai.test/api"}
    automation_allowed: {value: "yes", source: "https://openai.test/tos"}
    commercial_use: {value: "yes", source: "https://openai.test/tos"}
    watermark: C2PA metadata
    adapter: openai_images
    model: gpt-image-1
    api_key_env: OPENAI_API_KEY
    daily_quota: 10
    tos_reviewed: 2026-10-09
    domains: [openai.test]
"""


@pytest.fixture
def host() -> Iterator[ImageHost]:
    h = imagehost.start(ImageHost())
    yield h
    imagehost.stop(h)


def _with(studio_root: Path, *rows: str) -> Path:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + "".join(rows))
    return p


def _ledger(ctx: Context) -> list[tuple[str, str]]:
    return [
        (r["provider"], r["outcome"])
        for r in ctx.conn.execute("SELECT provider, outcome FROM job_attempts ORDER BY id")
    ]


class Scripted(FakeImageAdapter):
    def __init__(self, pid: str, *script: Exception) -> None:
        super().__init__(pid, "m")
        self.script = list(script)
        self.calls = 0

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        self.calls += 1
        if self.script:
            raise self.script.pop(0)
        return super().submit(request, staging_dir)


# ---- M1: an existing XMP packet ---------------------------------------------------


def _with_xmp(fmt: str) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (16, 8), (9, 9, 9))
    if fmt == "PNG":
        info = PngImagePlugin.PngInfo()
        info.add_itxt("XML:com.adobe.xmp", OTHER_XMP, zip=False)
        img.save(buf, "PNG", pnginfo=info)
    else:
        img.save(buf, "JPEG", xmp=OTHER_XMP.encode())
    return buf.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_m1_marking_merges_into_an_existing_xmp_packet(fmt: str) -> None:
    data = _with_xmp(fmt)
    marked = marker.mark_bytes(data)
    assert marker.bytes_are_marked(marked)
    assert b"provider-x" in marked, "the provider's own XMP is kept"
    # Exactly one packet, and Pillow (which reads the last one) sees the marker.
    with Image.open(io.BytesIO(marked)) as img:
        assert marker.DIGITAL_SOURCE_TYPE.encode() in img.info["xmp"]
        assert img.tobytes() == Image.open(io.BytesIO(data)).tobytes()


def test_m1_two_packets_are_ambiguous_and_never_read_as_marked() -> None:
    marked = marker.mark_bytes(_with_xmp("PNG"))
    head, rest = marked[:33], marked[33:]
    extra = marker._png_itxt(OTHER_XMP.encode())
    assert not marker.bytes_are_marked(head + rest[:0] + extra + rest)


def test_m1_a_marker_that_does_not_read_back_fails_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(marker, "merged_packet", lambda existing: OTHER_XMP.encode())
    with pytest.raises(PermanentProviderError, match="did not read back"):
        write_staged(tmp_path / "s", "job", _with_xmp("PNG"))
    assert not (tmp_path / "s").exists() or not any((tmp_path / "s").iterdir())


# ---- Low 7: a tEXt chunk is not an XMP container ---------------------------------


def test_low7_a_text_chunk_spoof_is_not_a_marker() -> None:
    packet = marker.XMP_PACKET.decode().replace("﻿", "")  # latin-1, so tEXt
    info = PngImagePlugin.PngInfo()
    info.add_text("XML:com.adobe.xmp", packet)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG", pnginfo=info)
    assert b"tEXt" in buf.getvalue()
    assert not marker.bytes_are_marked(buf.getvalue())


def test_low7_a_text_chunk_spoof_fails_d5(tmp_path: Path) -> None:
    from qontinui_studio.gates import d5_provenance
    from qontinui_studio.library import AssetRecord, VariantRecord
    from qontinui_studio.postprocess import sha256_file

    packet = marker.XMP_PACKET.decode().replace("﻿", "")
    info = PngImagePlugin.PngInfo()
    info.add_text("XML:com.adobe.xmp", packet)
    raw = tmp_path / "raw.png"
    Image.new("RGB", (8, 8)).save(raw, "PNG", pnginfo=info)
    var = tmp_path / "v.png"
    Image.new("RGB", (4, 4)).save(var, "PNG", **marker.save_options("png"))
    for f in (raw, var):
        provenance.write(
            f,
            provenance.build(
                file_sha256=sha256_file(f),
                file_name=f.name,
                role="raw",
                provider_id="p",
                provider_name="P",
                adapter="fake",
                model="m",
                terms={"commercial_use": "yes"},
                prompt="x",
                negative_prompt=None,
                references=[],
                seed=None,
                recipe=None,
                created_at="2026-10-15T00:00:00+00:00",
                job_id="j",
                batch_id="b",
                brief_id="a",
                facade=None,
            ),
        )
    asset = AssetRecord(
        uid="u", asset_id="a", brief_id="a", batch_id="b", job_id="j", provider="p",
        provider_name="P", model="m", prompt="x", created_at="t", raw_path=str(raw),
        sha256=sha256_file(raw), width=8, height=8, ai_generated=True,
        licence_commercial_use="yes", licence_source="https://x", licence_tos_reviewed="d",
        automation_allowed="yes", watermark="w", watermark_visible=False,
    )  # fmt: skip
    variant = VariantRecord(
        id="v", asset_uid="u", site="s", slot="hero", campaign=None, format="png",
        path=str(var), width=4, height=4, sha256=sha256_file(var),
    )  # fmt: skip
    result = d5_provenance(asset, variant)
    assert result.failed and "lacks the AI-generated marker" in result.detail


# ---- M2: bulk work never spills, even after a give-up ---------------------------


def _bulk(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    adapters: dict[str, FakeImageAdapter],
    priority: str = "bulk",
) -> tuple[Context, str]:
    _with(studio_root, LOCAL_ROW)
    path = write_brief(providers="local-gpu, fake")
    path.write_text(path.read_text() + f"priority: {priority}\n")
    ctx = make_context(adapters=AdapterSet(adapters=dict(adapters)))
    return ctx, new_batch(ctx, load_brief(path))


def test_m2_a_bulk_job_given_up_on_the_local_gpu_never_moves_to_a_metered_provider(
    studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    local = Scripted("local-gpu", ProviderDisabled("disabled (HTTP 503): switched off"))
    paid = Scripted("fake")  # metered, with a recorded quota
    ctx, batch = _bulk(studio_root, make_context, write_brief, {"local-gpu": local, "fake": paid})
    out = drive_batch(ctx, batch, publish=False)
    assert paid.calls == 0
    assert out.status == "failed"
    row = ctx.conn.execute("SELECT status, last_error FROM jobs").fetchone()
    assert row["status"] == "failed" and row["last_error"].startswith("bulk_no_spill:")


def test_m2_a_bulk_job_out_of_unknown_attempts_does_not_spill_either(
    studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    local = Scripted("local-gpu", *[ProviderOutcomeUnknown("dropped") for _ in range(8)])
    paid = Scripted("fake")
    ctx, batch = _bulk(studio_root, make_context, write_brief, {"local-gpu": local, "fake": paid})
    drive_batch(ctx, batch, publish=False)
    assert paid.calls == 0
    assert (
        ctx.conn.execute("SELECT last_error FROM jobs").fetchone()[0].startswith("bulk_no_spill:")
    )


def test_m2_an_interactive_job_may_still_move_on_within_quota(
    studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    local = Scripted("local-gpu", ProviderDisabled("disabled"))
    paid = Scripted("fake")
    ctx, batch = _bulk(
        studio_root,
        make_context,
        write_brief,
        {"local-gpu": local, "fake": paid},
        priority="interactive",
    )
    assert drive_batch(ctx, batch, publish=False).status == "generated"
    assert paid.calls == 1


# ---- M3: a host that keeps answering busy cannot hold a command forever ---------


def test_m3_repeated_busy_stops_at_the_wait_cap_and_reports_each_wait(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    busy = Scripted("fake", *[ProviderBusy("busy", 30.0) for _ in range(500)])
    waits: list[tuple[float, str]] = []
    ctx = make_context(
        adapters=AdapterSet(adapters={"fake": busy}),
        on_wait=lambda s, why: waits.append((s, why)),
    )
    out = drive_batch(ctx, new_batch(ctx, load_brief(write_brief(providers="fake"))), False)
    assert out.status == "generating"
    assert out.stopped_waiting is not None and "stopped after waiting 1800s" in out.stopped_waiting
    assert len(waits) == 60 and all(s == 30.0 and "busy" in why for s, why in waits)
    assert busy.calls == 61


@pytest.mark.local_http
def test_m3_the_cli_prints_each_wait_and_exits_3(
    host: ImageHost,
    studio_root: Path,
    write_brief: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(pipeline, "utc_now", clock)
    monkeypatch.setattr(pipeline.time, "sleep", clock.sleep)
    _with(studio_root, LOCAL_ROW.replace("http://127.0.0.1:8100/v1", host.v1_url))
    monkeypatch.setenv("STUDIO_TEST_FACADE_KEY", "k")
    host.queue(*[imagehost.refusal(503, "busy", 600) for _ in range(5)])
    brief = write_brief(providers="local-gpu")
    r = CliRunner().invoke(cli.main, ["--root", str(studio_root), "brief", "run", "--no-publish", str(brief)])  # fmt: skip
    assert r.exit_code == 3, r.output
    assert r.output.count("waiting 600s:") == 3
    assert "stopped after waiting 1800s" in r.output
    assert "last attempt: busy from local-gpu" in r.output


# ---- M4: jobs claimed before schema v2 --------------------------------------------


def _v1_db_with_inflight_jobs(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> tuple[Path, str]:
    ctx = make_context()
    batch = new_batch(ctx, load_brief(write_brief(variants=2)))
    req = {batch: _request_for(load_brief(write_brief(variants=2)))}
    sched = ctx.scheduler()
    sched.step(req)
    clock.sleep(10)  # past the poll interval
    sched.step(req)
    ctx.conn.execute(
        "UPDATE jobs SET status = 'submitted', files_json = NULL, polls = 0 WHERE id = ?",
        (f"{batch}-v1",),
    )
    ctx.conn.commit()
    statuses = sorted(r[0] for r in ctx.conn.execute("SELECT status FROM jobs"))
    assert statuses == ["downloaded", "submitted"]
    db = ctx.workspace.db_path
    ctx.conn.close()
    raw = sqlite3.connect(db)  # back to the v1 shape: what a v1 studio.db holds
    raw.execute("DROP INDEX job_attempts_job")
    raw.execute("DROP TABLE job_attempts")
    for col in ("quota_day", "adapter", "sent_json", "home_provider"):
        raw.execute(f"ALTER TABLE jobs DROP COLUMN {col}")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    return db, batch


def _sidecars_complete(ctx: Context) -> int:
    """Every file the library records (raw and variants) has a complete sidecar."""
    paths = [r[0] for r in ctx.conn.execute("SELECT raw_path FROM assets")]
    paths += [r[0] for r in ctx.conn.execute("SELECT path FROM variants")]
    for f in map(Path, paths):
        record = provenance.read(f)
        assert record is not None and provenance.missing(record) == [], f
        assert record["provider"]["adapter"] == "fake"
    return len(paths)


def test_m4_the_migration_backfills_the_adapter_of_inflight_jobs(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
) -> None:
    db, batch = _v1_db_with_inflight_jobs(make_context, write_brief, clock)
    inv = load_providers(studio_root / "providers.yaml")
    conn = connect(db, inv.adapter_kinds())
    assert [r[0] for r in conn.execute("SELECT adapter FROM jobs")] == ["fake", "fake"]
    conn.close()
    ctx = make_context()
    assert drive_batch(ctx, batch, publish=False).status == "generated"
    assert _sidecars_complete(ctx) == 12  # 2 raw + 2 x 5 derivatives


def test_m4_ingest_resolves_an_adapter_the_migration_could_not_backfill(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
) -> None:
    db, batch = _v1_db_with_inflight_jobs(make_context, write_brief, clock)
    ctx = make_context()  # migrates with no inventory: adapter stays NULL
    assert [r[0] for r in ctx.conn.execute("SELECT adapter FROM jobs")] == [None, None]
    assert drive_batch(ctx, batch, publish=False).status == "generated"
    assert _sidecars_complete(ctx) == 12


# ---- L1: vendor keys never reach a gateway route --------------------------------


def _gateway_inventory(studio_root: Path) -> object:
    return load_providers(_with(studio_root, LOCAL_ROW, OPENAI_ROW))


@respx.mock
def test_l1_a_vendor_key_is_never_sent_to_a_gateway_route(
    studio_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-vendor")
    monkeypatch.setenv("STUDIO_TEST_FACADE_KEY", "facade-key")
    inv = _gateway_inventory(studio_root)
    route = "https://10.1.2.3/v1"  # local, so the unmetered row may ride it
    built = build_adapters(inv, gateway=GatewayState("declared", "runner", "", route))  # type: ignore[arg-type]
    assert "openai" not in built.adapters
    assert "vendor key is never sent" in built.unavailable["openai"]
    respx.post(f"{route}/images/generations").mock(
        return_value=httpx.Response(503, json={"error": {"type": "busy"}})
    )
    local = built.adapters["local-gpu"]
    with pytest.raises(ProviderBusy):
        local.submit(GenerationRequest("j", "p", "1:1"), tmp_path)
    assert "authorization" not in {k.lower() for k in respx.calls.last.request.headers}

    monkeypatch.setenv("QONTINUI_GATEWAY_IMAGES_KEY", "gw-secret")
    built = build_adapters(
        inv,  # type: ignore[arg-type]
        gateway=GatewayState("declared", "runner", "", route, "QONTINUI_GATEWAY_IMAGES_KEY"),
    )
    oa = built.adapters["openai"]
    assert isinstance(oa, OpenAIImageAdapter)
    respx.post(f"{route}/images/generations").mock(
        return_value=httpx.Response(400, json={"error": {"message": "x"}})
    )
    with pytest.raises(PermanentProviderError):
        oa.submit(GenerationRequest("j", "p", "1:1"), tmp_path)
    assert respx.calls.last.request.headers["authorization"] == "Bearer gw-secret"


@pytest.mark.local_http
@pytest.mark.parametrize(
    ("route", "state"),
    [
        ("http://example.com/v1", "unknown"),
        ("http://10.0.0.5:9000/v1", "declared"),
        ("https://gw.example.com/v1", "declared"),
        ("ftp://10.0.0.5/v1", "unknown"),
    ],
)
def test_l1_only_an_https_or_literal_local_route_is_accepted(
    host: ImageHost, route: str, state: str
) -> None:
    host.queue(
        imagehost.runner_health(),
        imagehost.json_reply(200, {"state": "declared", "routes": {"images": route}}),
    )
    got = read_gateway_state(host.v1_url.removesuffix("/v1"))
    assert got.state == state
    assert (got.refusal() is None) is (state == "declared")


def test_l1_a_non_loopback_runner_url_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    # No connection is even attempted (conftest refuses non-loopback sockets).
    got = read_gateway_state("http://10.9.9.9:9876")
    assert got.state == "unknown" and got.source == "untrusted_runner_url"
    assert got.refusal() is not None


# ---- Low 3: retry_after is bounded ------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(float("inf"), 30.0), (float("nan"), 30.0), (-5, 30.0), (1e12, MAX_RETRY_AFTER_S), (0, 1.0)],
)
def test_low3_retry_after_is_clamped(value: float, expected: float) -> None:
    assert ProviderBusy("busy", value).retry_after_s == expected


@pytest.mark.local_http
def test_low3_a_non_finite_host_retry_after_is_ignored(host: ImageHost, tmp_path: Path) -> None:
    host.queue(
        imagehost.json_reply(503, {"error": {"type": "busy", "retry_after_s": float("inf")}}),
        imagehost.json_reply(503, {"error": {"type": "busy"}}, {"Retry-After": "inf"}),
    )
    adapter = OpenAICompatibleImageAdapter("local-gpu", "z", host.v1_url)
    for _ in range(2):
        with pytest.raises(ProviderBusy) as info:
            adapter.submit(GenerationRequest("j", "p", "1:1"), tmp_path)
        assert info.value.retry_after_s == 30.0


def test_low3_the_scheduler_survives_an_infinite_retry_after(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    exc = ProviderBusy("busy", 1.0)
    exc.retry_after_s = float("inf")  # bypassing the constructor's clamp
    ctx = make_context(adapters=AdapterSet(adapters={"fake": Scripted("fake", exc)}))
    batch = new_batch(ctx, load_brief(write_brief(providers="fake")))
    ctx.scheduler().step({batch: _request_for(load_brief(write_brief(providers="fake")))})
    row = ctx.conn.execute("SELECT status, next_attempt_at FROM jobs").fetchone()
    assert row["status"] == "queued" and row["next_attempt_at"] > clock.now.isoformat()


# ---- Low 4: a stream that never ends is cut at a wall-clock deadline ------------


@pytest.mark.local_http
def test_low4_a_never_ending_progress_stream_hits_the_deadline(
    host: ImageHost, tmp_path: Path
) -> None:
    host.queue(imagehost.trickle_reply(200, 0.05))
    adapter = OpenAICompatibleImageAdapter("local-gpu", "z", host.v1_url, deadline_s=0.5)
    started = time.monotonic()
    with pytest.raises(ProviderOutcomeUnknown, match="deadline"):
        adapter.submit(GenerationRequest("j", "p", "1:1"), tmp_path)
    assert time.monotonic() - started < 5.0


def test_low4_the_deadline_fits_inside_the_claim_lease() -> None:
    from qontinui_studio.scheduler import RetryPolicy, Scheduler

    adapter = OpenAICompatibleImageAdapter("local-gpu", "z", "http://127.0.0.1:1/v1")
    assert adapter.hard_timeout_s >= adapter._deadline_s + READ_TIMEOUT_S + CONNECT_TIMEOUT_S
    lease = Scheduler._lease(None, adapter.hard_timeout_s, RetryPolicy().submit_stale_s)  # type: ignore[arg-type]
    assert lease > adapter.hard_timeout_s


# ---- Low 5: an interactive fallback never ping-pongs -----------------------------


def test_low5_a_fallback_never_returns_to_the_provider_it_left(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    a = Scripted("fake", *[ProviderBusy("busy", 60.0) for _ in range(20)])
    b = Scripted("fake2", *[ProviderBusy("busy", 60.0) for _ in range(20)])
    ctx = make_context(adapters=AdapterSet(adapters={"fake": a, "fake2": b}))
    path = write_brief(providers="fake, fake2")
    batch = new_batch(ctx, load_brief(path))
    req = {batch: _request_for(load_brief(path))}
    for _ in range(4):
        ctx.scheduler().step(req)
        clock.sleep(61)
    assert a.calls == 1, "the provider left behind is never called again"
    assert _ledger(ctx)[:2] == [("fake", "busy"), ("fake2", "busy")]
    assert json.loads(ctx.conn.execute("SELECT tried_providers FROM jobs").fetchone()[0]) == [
        "fake"
    ]


# ---- Low 6: launch build/run exit 6 on a gateway refusal ------------------------


@pytest.mark.parametrize("command", ["build", "run"])
def test_low6_launch_exits_6_on_a_gateway_refusal(
    studio_root: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setattr(cli, "read_gateway_state", lambda: GatewayState("declared", "runner"))
    product = write_product(studio_root, capture=False)
    r = CliRunner().invoke(cli.main, ["--root", str(studio_root), "launch", command, str(product)])
    assert r.exit_code == 6, r.output
    assert "gateway_has_no_image_route" in r.output and "--no-generate" in r.output


# ---- Low 1 at the adapter level ---------------------------------------------------


def test_low1_cloud_wires_report_no_sent_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    req = GenerationRequest("j", "p", "1:1", negative_prompt="n", seed=3, recipe="hero")
    assert OpenAIImageAdapter("openai", "gpt-image-1").sent_parameters(req) == {}
    assert FakeImageAdapter().sent_parameters(req) == {}
    local = OpenAICompatibleImageAdapter("local-gpu", "z", "http://127.0.0.1:1/v1")
    assert local.sent_parameters(req) == {
        "priority": "interactive",
        "seed": 3,
        "recipe": "hero",
        "negative_prompt": "n",
    }


# ---- a bulk busy never moves, not even to another free local host ---------------


def test_a_bulk_busy_stays_on_its_host_even_with_another_unmetered_host(
    studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    second = (
        LOCAL_ROW.replace("id: local-gpu", "id: local-gpu2")
        .replace("local.test", "local2.test")
        .replace("provider: Local image facade", "provider: Second local facade")
        .replace("    api_key_env: STUDIO_TEST_FACADE_KEY\n", "")
    )
    _with(studio_root, LOCAL_ROW, second)
    path = write_brief(providers="local-gpu, local-gpu2")
    path.write_text(path.read_text() + "priority: bulk\n")
    first = Scripted("local-gpu", ProviderBusy("busy", 120.0))
    other = Scripted("local-gpu2")
    ctx = make_context(adapters=AdapterSet(adapters={"local-gpu": first, "local-gpu2": other}))
    batch = new_batch(ctx, load_brief(path))
    ctx.scheduler().step({batch: _request_for(load_brief(path))})
    row = ctx.conn.execute("SELECT provider, tried_providers FROM jobs").fetchone()
    assert (row["provider"], row["tried_providers"], other.calls) == ("local-gpu", "[]", 0)
