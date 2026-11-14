"""The image-generation off-switches (plan 2026-10-13 Phase 12 steps 3 and 4):
coord's ``image_generation`` dial and this machine's ``QONTINUI_IMAGEGEN``.

* ``off`` refuses every generation path with a typed reason, before any
  provider call, and leaves the batch ``generating`` so it resumes;
* ``local_only`` refuses every cloud adapter and every adapter riding a gateway
  route, and admits the fleet's own unmetered local host;
* an unreadable dial is the last level coord answered, else ``local_only`` —
  never the ``on`` default;
* within one poll of the dial changing, a running batch follows it.

The CLI tests read the dial through the real reader from a fake coord on a
loopback socket (no production coord row is ever written).
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from click.testing import CliRunner

from qontinui_studio import device_auth, imagegen_policy
from qontinui_studio.adapters import AdapterSet, build_adapters
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.cli import main
from qontinui_studio.errors import ImageGenerationRefused
from qontinui_studio.gateway import GatewayState
from qontinui_studio.imagegen_policy import (
    CoordDialReader,
    DialReading,
    ImagegenPolicy,
    PolicyDecision,
    restrict,
)
from qontinui_studio.pipeline import (
    Context,
    _request_for,
    drive_batch,
    new_batch,
    resume_all,
    run_brief,
)
from qontinui_studio.providers import load_providers

from .conftest import FakeClock

COORD = "http://coord.test"
TOKEN = "header.payload.sig"

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


def _jwt(exp: object, **claims: object) -> str:
    raw = json.dumps({"exp": exp, **claims}).encode()  # NaN/Infinity allowed
    payload = base64.urlsafe_b64encode(raw).rstrip(b"=")
    return f"h.{payload.decode()}.s"


TENANT = "c231d9da-0ca8-4fe4-bd81-0e3d6c20339a"
MINT_URL = "http://127.0.0.1:9876/ui-bridge/invoke/get_coord_device_token"


class Dial:
    """A scripted dial: the level coord would answer, or None (unreadable)."""

    def __init__(self, level: str | None = "on") -> None:
        self.level = level
        self.reads: list[str | None] = []

    def __call__(self, repo: str | None) -> DialReading:
        self.reads.append(repo)
        if self.level is None:
            return DialReading(None, "test: coord unreachable")
        return DialReading(self.level, "test", "tenant")  # type: ignore[arg-type]


def _with(studio_root: Path, *rows: str) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + "".join(rows))


def _repo(studio_root: Path, site: str, repo: str) -> None:
    p = studio_root / "sites" / f"{site}.yaml"
    p.write_text(p.read_text().replace("kind: demo", f"kind: demo\nrepo: {repo}", 1))


def _attempts(ctx: Context) -> int:
    return int(ctx.conn.execute("SELECT COUNT(*) FROM job_attempts").fetchone()[0])


def _status(ctx: Context, batch: str) -> str:
    return str(ctx.conn.execute("SELECT status FROM batches WHERE id = ?", (batch,)).fetchone()[0])


class Counting(FakeImageAdapter):
    def __init__(self, pid: str, pending_polls: int = 0) -> None:
        super().__init__(pid, "m", pending_polls=pending_polls)
        self.calls = 0

    def submit(self, request: Any, staging_dir: Path) -> Any:
        self.calls += 1
        return super().submit(request, staging_dir)


# ---------------------------------------------------------------------------
# The reader: credentials, the wire, and what counts as unreadable
# ---------------------------------------------------------------------------


@respx.mock
def test_the_reader_names_the_domain_and_repo_and_parses_the_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COORD_DEVICE_JWT", TOKEN)
    route = respx.get(f"{COORD}/coord/fleet-policy").mock(
        return_value=httpx.Response(
            200,
            json={
                "domain": "image_generation",
                "effective_level": "local_only",
                "resolved_scope": "repo",
            },
        )
    )
    reading = CoordDialReader(COORD, jwt_file=tmp_path / "none")("Acme/App")
    assert reading == DialReading("local_only", "coord answered", "repo", "$COORD_DEVICE_JWT")
    sent = route.calls.last.request
    assert parse_qs(urlsplit(str(sent.url)).query) == {
        "domain": ["image_generation"],
        "repo": ["Acme/App"],
    }
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    # A tenant-wide read sends no repo.
    CoordDialReader(COORD, jwt_file=tmp_path / "none")(None)
    assert "repo" not in parse_qs(urlsplit(str(route.calls.last.request.url)).query)


@respx.mock
def test_the_credential_chain_falls_through_on_validity_not_presence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jwt_file = tmp_path / "coord-device-jwt"
    live = _jwt(time.time() + 3600)
    jwt_file.write_text(live + "\n")
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer rejected.by.coord":
            return httpx.Response(401)
        return httpx.Response(200, json={"effective_level": "on", "resolved_scope": "none"})

    respx.get(f"{COORD}/coord/fleet-policy").mock(side_effect=answer)

    # An EXPIRED env token is skipped without being sent; the file's is used.
    monkeypatch.setenv("COORD_DEVICE_JWT", _jwt(time.time() - 60))
    r = CoordDialReader(COORD, jwt_file=jwt_file)(None)
    assert (r.level, r.credential) == ("on", str(jwt_file))
    assert seen == [f"Bearer {live}"]

    # A token coord REFUSES is passed over for the next.
    seen.clear()
    monkeypatch.setenv("COORD_DEVICE_JWT", "rejected.by.coord")
    r = CoordDialReader(COORD, jwt_file=jwt_file)(None)
    assert (r.level, r.credential) == ("on", str(jwt_file))
    assert seen == ["Bearer rejected.by.coord", f"Bearer {live}"]

    # Nothing usable: unreadable, naming every source it passed over. The runner
    # mint is never used for a coord other than production.
    monkeypatch.delenv("COORD_DEVICE_JWT")
    jwt_file.unlink()
    r = CoordDialReader(COORD, jwt_file=jwt_file)(None)
    assert r.level is None
    assert "$COORD_DEVICE_JWT unset" in r.reason and "absent or empty" in r.reason
    assert "runner mint not used" in r.reason


@respx.mock
def test_the_runner_mint_is_the_last_rung_for_production_coord(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("COORD_DEVICE_JWT", raising=False)
    monkeypatch.setenv("STUDIO_COORD_TENANT_ID", TENANT)
    minted = _jwt(time.time() + 3600, tenant_id=TENANT.upper())
    mint = respx.post(MINT_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": minted})
    )
    read = respx.get("https://coord.qontinui.io/coord/fleet-policy").mock(
        return_value=httpx.Response(
            200, json={"effective_level": "off", "resolved_scope": "tenant"}
        )
    )
    r = CoordDialReader(
        "https://coord.qontinui.io",
        jwt_file=tmp_path / "none",
        runner=lambda: "http://127.0.0.1:9876",
    )(None)
    assert (r.level, r.credential) == ("off", "runner mint")
    assert json.loads(mint.calls.last.request.content) == {
        "args": {"tenantId": "c231d9da-0ca8-4fe4-bd81-0e3d6c20339a"}
    }
    assert read.calls.last.request.headers["Authorization"] == f"Bearer {minted}"
    # A runner URL off this machine is never asked — including a NAME that only
    # looks like a loopback address.
    for url in ("http://10.0.0.5:9876", "http://127.evil.example:9876", "http://127.0.0.1.nip.io"):
        r = CoordDialReader(
            "https://coord.qontinui.io", jwt_file=tmp_path / "none", runner=lambda u=url: u
        )(None)
        assert r.level is None and "not loopback" in r.reason, url
    assert mint.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    ("token", "why"),
    [
        (_jwt(time.time() + 3600, tenant_id="0e7c0000-0000-0000-0000-000000000000"), "tenant_id"),
        (_jwt(time.time() + 3600), "tenant_id"),
        (_jwt(time.time() - 60, tenant_id=TENANT), "expired"),
        (_jwt(float("nan"), tenant_id=TENANT), "expired"),
        (_jwt("tomorrow", tenant_id=TENANT), "expired"),
    ],
    ids=["other-tenant", "no-tenant-claim", "expired", "nan-exp", "string-exp"],
)
def test_a_minted_token_for_another_tenant_or_expired_is_discarded(
    token: str, why: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("COORD_DEVICE_JWT", raising=False)
    monkeypatch.setenv("STUDIO_COORD_TENANT_ID", TENANT)
    respx.post(MINT_URL).mock(return_value=httpx.Response(200, json={"data": token}))
    read = respx.get("https://coord.qontinui.io/coord/fleet-policy").mock(
        return_value=httpx.Response(200, json={"effective_level": "on"})
    )
    r = CoordDialReader(
        "https://coord.qontinui.io",
        jwt_file=tmp_path / "none",
        runner=lambda: "http://127.0.0.1:9876",
    )(None)
    assert r.level is None and why in r.reason
    assert read.call_count == 0, "a discarded token is never sent to coord"


def test_only_literal_loopback_or_localhost_counts_as_loopback() -> None:
    loop = device_auth.is_loopback_url
    assert loop("http://localhost:9876") and loop("http://127.0.0.2:9876")
    assert loop("http://[::1]:9876")
    assert not loop("http://127.evil.example:9876") and not loop("http://localhost.evil:1")


def test_a_non_numeric_exp_counts_as_expired() -> None:
    assert device_auth.jwt_expired(_jwt(float("nan")))
    assert device_auth.jwt_expired(_jwt(float("inf")))
    assert device_auth.jwt_expired(_jwt("9999999999"))
    assert device_auth.jwt_expired(_jwt(True))
    assert not device_auth.jwt_expired(_jwt(time.time() + 60))
    assert not device_auth.jwt_expired("not-a-jwt")


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="down"),
        httpx.Response(400, json={"error": "repo_not_owner_name"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"effective_level": "local-only"}),
        httpx.Response(200, json={"effective_level": None}),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
    ids=["503", "400", "garbage", "mistyped", "null", "refused", "timeout"],
)
def test_anything_but_one_of_the_three_levels_is_unreadable(
    response: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COORD_DEVICE_JWT", TOKEN)
    route = respx.get(f"{COORD}/coord/fleet-policy")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    assert CoordDialReader(COORD, jwt_file=tmp_path / "none")("acme/app").level is None


# ---------------------------------------------------------------------------
# Decisions: the machine switch, last known, the unreadable default
# ---------------------------------------------------------------------------


def test_the_machine_switch_is_off_without_reading_coord(
    make_context: Callable[..., Context], monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    ctx = make_context()
    dial = Dial("on")
    policy = ImagegenPolicy(dial)
    monkeypatch.setenv("QONTINUI_IMAGEGEN", "0")
    d = policy.decide(ctx.conn, {"acme/app"}, clock())
    assert (d.level, d.source) == ("off", "machine_switch") and dial.reads == []
    assert d.refusal() is not None and "on this machine" in str(d.refusal())
    # A mistyped switch is off too, and says so.
    monkeypatch.setenv("QONTINUI_IMAGEGEN", "false")
    d = policy.decide(ctx.conn, {None}, clock())
    assert d.level == "off" and "neither 0 nor 1" in d.reason
    # 1 and unset leave the dial in charge.
    for value in ("1", ""):
        monkeypatch.setenv("QONTINUI_IMAGEGEN", value)
        assert policy.decide(ctx.conn, {None}, clock()).source == "coord"


def test_unreadable_is_the_last_level_coord_answered_else_local_only(
    make_context: Callable[..., Context], clock: FakeClock
) -> None:
    ctx = make_context()
    # Never read: local_only, never the `on` default.
    d = ImagegenPolicy(Dial(None)).decide(ctx.conn, {"acme/app"}, clock())
    assert (d.level, d.source) == ("local_only", "unreadable")
    assert "never on" in d.reason

    # A later run (a new policy: a CLI run has no memory) reads `off`...
    assert ImagegenPolicy(Dial("off")).decide(ctx.conn, {"Acme/App"}, clock()).level == "off"
    # ...and the run after that cannot reach coord: the last answer governs,
    # matched ignoring case, and only for that repo.
    d = ImagegenPolicy(Dial(None)).decide(ctx.conn, {"acme/app"}, clock())
    assert (d.level, d.source) == ("off", "last_known")
    assert d.refusal() is not None
    other = ImagegenPolicy(Dial(None)).decide(ctx.conn, {"acme/other"}, clock())
    assert (other.level, other.source) == ("local_only", "unreadable")

    # An unreadable dial never overwrites what coord last said.
    ImagegenPolicy(Dial(None)).decide(ctx.conn, {"acme/app"}, clock())
    row = ctx.conn.execute(
        "SELECT level FROM imagegen_dial_last_known WHERE repo_key = 'acme/app'"
    ).fetchone()
    assert row[0] == "off"


def test_a_batch_takes_the_most_restrictive_level_of_its_repos(
    make_context: Callable[..., Context], clock: FakeClock
) -> None:
    ctx = make_context()
    levels = {"acme/a": "on", "acme/b": "local_only", "": "on"}

    def reader(repo: str | None) -> DialReading:
        return DialReading(levels[(repo or "").lower()], "test", "repo")  # type: ignore[arg-type]

    d = ImagegenPolicy(reader).decide(ctx.conn, {"acme/a", "acme/b", None}, clock())
    assert (d.level, d.repo) == ("local_only", "acme/b")
    levels[""] = "off"
    d = ImagegenPolicy(reader).decide(ctx.conn, {"acme/a", None}, clock())
    assert (d.level, d.repo) == ("off", None)


def test_a_reading_is_reused_for_one_poll_then_read_again(
    make_context: Callable[..., Context], clock: FakeClock
) -> None:
    ctx = make_context()
    dial = Dial("on")
    policy = ImagegenPolicy(dial, poll_interval_s=60)
    policy.decide(ctx.conn, {"acme/app"}, clock())
    dial.level = "off"
    clock.sleep(59)
    assert policy.decide(ctx.conn, {"ACME/app"}, clock()).level == "on"
    clock.sleep(1)
    assert policy.decide(ctx.conn, {"acme/app"}, clock()).level == "off"
    assert dial.reads == ["acme/app", "acme/app"]


# ---------------------------------------------------------------------------
# Enforcement: which adapters each level admits
# ---------------------------------------------------------------------------


def _template(loaded: Any) -> Any:
    return _request_for(loaded)[1]


def _decision(level: str) -> PolicyDecision:
    return PolicyDecision(level, "coord", "test")  # type: ignore[arg-type]


def test_local_only_admits_the_fleets_own_host_and_refuses_cloud(
    studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _with(studio_root, LOCAL_ROW, OPENAI_ROW)
    inv = load_providers(studio_root / "providers.yaml")
    adapters = build_adapters(inv)
    assert {"local-gpu", "openai", "fake"} <= set(adapters.adapters)

    assert restrict(adapters, _decision("on"), inv) is adapters
    local = restrict(adapters, _decision("local_only"), inv, ["openai", "local-gpu"])
    assert set(local.adapters) == {"local-gpu", "fake", "fake2"}
    assert "local_only" in local.unavailable["openai"]
    assert "a cloud host" in local.unavailable["openai"]

    # A brief whose every built provider is cloud is refused, not failed.
    with pytest.raises(ImageGenerationRefused) as e:
        restrict(adapters, _decision("local_only"), inv, ["openai"])
    assert e.value.code == "image_generation_local_only"

    with pytest.raises(ImageGenerationRefused) as e:
        restrict(adapters, _decision("off"), inv, ["local-gpu"])
    assert e.value.code == "image_generation_off"


def test_local_only_refuses_every_adapter_riding_a_gateway_route(studio_root: Path) -> None:
    _with(studio_root, LOCAL_ROW)
    inv = load_providers(studio_root / "providers.yaml")
    gateway = GatewayState("declared", "runner", "", "http://127.0.0.1:7000/v1")
    adapters = build_adapters(inv, gateway=gateway)
    assert adapters.route == "http://127.0.0.1:7000/v1" and "local-gpu" in adapters.adapters
    local = restrict(adapters, _decision("local_only"), inv)
    assert local.adapters == {}
    assert "through the gateway route" in local.unavailable["local-gpu"]


# ---------------------------------------------------------------------------
# The pipeline: every generation path consults the switches
# ---------------------------------------------------------------------------


def test_off_refuses_brief_run_before_a_batch_or_a_provider_call(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    fake = Counting("fake")
    ctx = make_context(imagegen=ImagegenPolicy(Dial("off")))
    ctx.adapters.adapters["fake"] = fake
    with pytest.raises(ImageGenerationRefused) as e:
        run_brief(ctx, write_brief())
    assert e.value.code == "image_generation_off"
    assert fake.calls == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_off_refuses_a_resumed_batch_and_on_resumes_it(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    dial = Dial("on")
    fake = Counting("fake")
    ctx = make_context(imagegen=ImagegenPolicy(dial, poll_interval_s=0))
    ctx.adapters.adapters["fake"] = fake
    batch = new_batch(ctx, load_brief(write_brief()), publish=False)

    dial.level = "off"
    with pytest.raises(ImageGenerationRefused):
        drive_batch(ctx, batch, publish=False)
    [refused] = resume_all(ctx, publish=False)
    assert refused.refused is not None and refused.refused.code == "image_generation_off"
    assert fake.calls == 0 and _attempts(ctx) == 0
    assert _status(ctx, batch) == "generating"
    # The lease was released: the batch is drivable again.
    assert (
        ctx.conn.execute("SELECT driver FROM batches WHERE id = ?", (batch,)).fetchone()[0] is None
    )

    dial.level = "on"
    [done] = resume_all(ctx, publish=False)
    assert done.status == "generated" and fake.calls == 1
    assert done.imagegen is not None and done.imagegen.startswith("on")


def test_a_running_batch_stops_within_one_poll_of_the_dial_going_off(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    dial = Dial("on")
    ctx = make_context(imagegen=ImagegenPolicy(dial, poll_interval_s=0))
    calls: list[str] = []

    class Watched(FakeImageAdapter):
        def submit(self, request: Any, staging_dir: Path) -> Any:
            calls.append("submit")
            return super().submit(request, staging_dir)

        def poll(self, job: Any) -> Any:
            calls.append("poll")
            return super().poll(job)

    ctx.adapters.adapters["fake"] = Watched("fake", "m", pending_polls=1)

    calls_at_flip: list[str] = []

    def sleep(seconds: float) -> None:
        dial.level = "off"  # the operator flips the dial while studio waits
        calls_at_flip[:] = calls
        clock.sleep(seconds)

    ctx.sleep = sleep
    with pytest.raises(ImageGenerationRefused):
        run_brief(ctx, write_brief(), publish=False)
    assert calls_at_flip and calls_at_flip[0] == "submit", "the job was submitted while on"
    assert calls == calls_at_flip, "no provider call after the dial went off"
    [batch] = [r[0] for r in ctx.conn.execute("SELECT id FROM batches")]
    assert _status(ctx, batch) == "generating"


@pytest.mark.parametrize("poll_interval_s", [0.0, 60.0])
def test_the_dial_is_checked_before_every_provider_call_within_one_step(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
    poll_interval_s: float,
) -> None:
    """Four variants are due in ONE scheduler step. The dial flips off during
    the first submit (a slow cloud call: 61 s pass, past the 60 s cache): no
    other variant is submitted, and the batch is not generated."""
    dial = Dial("on")
    ctx = make_context(imagegen=ImagegenPolicy(dial, poll_interval_s=poll_interval_s))
    submits: list[str] = []

    class Slow(FakeImageAdapter):
        def submit(self, request: Any, staging_dir: Path) -> Any:
            submits.append(request.job_id)
            clock.sleep(61)
            dial.level = "off"
            return super().submit(request, staging_dir)

    ctx.adapters.adapters["fake"] = Slow("fake", "m")
    with pytest.raises(ImageGenerationRefused):
        run_brief(ctx, write_brief(variants=4), publish=False)
    assert len(submits) == 1, f"submits after the dial went off: {submits[1:]}"
    [batch] = [r[0] for r in ctx.conn.execute("SELECT id FROM batches")]
    assert _status(ctx, batch) == "generating"
    queued = ctx.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND provider IS NULL"
    ).fetchone()[0]
    assert queued == 3


def test_a_batch_with_no_job_due_is_still_refused_typed(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    dial = Dial("on")
    ctx = make_context(imagegen=ImagegenPolicy(dial, poll_interval_s=0))
    batch = new_batch(ctx, load_brief(write_brief()), publish=False)
    later = (clock() + timedelta(hours=6)).isoformat()
    ctx.conn.execute("UPDATE jobs SET next_attempt_at = ?", (later,))
    ctx.conn.commit()
    dial.level = "off"
    with pytest.raises(ImageGenerationRefused):
        drive_batch(ctx, batch, publish=False)


def test_local_only_still_collects_a_job_already_submitted_to_a_cloud_provider(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _with(studio_root, OPENAI_ROW)
    inv = load_providers(studio_root / "providers.yaml")
    dial = Dial("on")
    ctx = make_context(inventory=inv, imagegen=ImagegenPolicy(dial, poll_interval_s=0))
    ctx.adapters = build_adapters(inv)
    cloud = Counting("openai", pending_polls=1)
    ctx.adapters.adapters["openai"] = cloud
    loaded = load_brief(write_brief(providers="openai, fake", variants=2))
    batch = new_batch(ctx, loaded, publish=False)
    # One variant is submitted to the cloud while the dial is on...
    one = ctx.conn.execute("SELECT id FROM jobs ORDER BY id").fetchall()
    ctx.conn.execute(
        "UPDATE jobs SET next_attempt_at = ? WHERE id = ?",
        ((ctx.clock() + timedelta(hours=1)).isoformat(), one[1]["id"]),
    )
    ctx.conn.commit()
    ctx.scheduler().step({batch: (loaded.brief.provider_preference, _template(loaded))})
    assert cloud.calls == 1
    in_flight = ctx.conn.execute(
        "SELECT status, provider FROM jobs WHERE id = ?", (one[0]["id"],)
    ).fetchone()
    assert tuple(in_flight) == ("submitted", "openai"), "the cloud job is still in flight"
    ctx.conn.execute("UPDATE jobs SET next_attempt_at = ?", (ctx.clock().isoformat(),))
    ctx.conn.commit()
    # ...then the dial goes local_only: that job is still collected from the
    # cloud (paid work is not lost); the other variant goes to the local host.
    dial.level = "local_only"
    out = drive_batch(ctx, batch, publish=False)
    assert out.status == "generated", out
    assert cloud.calls == 1, "no NEW submission to the cloud under local_only"
    providers = dict(ctx.conn.execute("SELECT id, provider FROM jobs").fetchall())
    assert providers[one[0]["id"]] == "openai", "collected from the cloud, not re-generated"
    assert sorted(providers.values()) == ["fake", "openai"]


@respx.mock
def test_local_only_never_calls_a_cloud_provider_and_generates_locally(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _with(studio_root, OPENAI_ROW)
    cloud = respx.route(host="api.openai.com").mock(return_value=httpx.Response(500))
    inv = load_providers(studio_root / "providers.yaml")
    ctx = make_context(inventory=inv, imagegen=ImagegenPolicy(Dial("local_only")))
    ctx.adapters = build_adapters(inv)
    assert "openai" in ctx.adapters.adapters
    out = run_brief(ctx, write_brief(providers="openai, fake"), publish=False)
    assert out.status == "generated"
    assert cloud.call_count == 0
    assert "local_only" in out.unavailable_providers["openai"]
    assert {r[0] for r in ctx.conn.execute("SELECT provider FROM jobs")} == {"fake"}


def test_the_dial_is_read_for_each_target_sites_repo(
    studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    _repo(studio_root, "cafe", "acme/cafe-site")
    dial = Dial("on")
    ctx = make_context(imagegen=ImagegenPolicy(dial))
    run_brief(ctx, write_brief(), publish=False)
    # cafe names its repo; barber names none, so the tenant-wide level is read.
    assert set(dial.reads) == {"acme/cafe-site", None}


# ---------------------------------------------------------------------------
# The CLI, against a fake coord through the real reader (the Arming)
# ---------------------------------------------------------------------------


@dataclass
class FakeCoord:
    levels: dict[str, str] = field(default_factory=dict)  # repo ("" tenant) -> level
    status: int = 200
    requests: list[dict[str, Any]] = field(default_factory=list)
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def coord() -> Iterator[FakeCoord]:
    fake = FakeCoord()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            fake.requests.append(
                {"path": parts.path, "query": q, "auth": self.headers.get("Authorization")}
            )
            if fake.status != 200 or self.headers.get("Authorization") != f"Bearer {TOKEN}":
                code = fake.status if fake.status != 200 else 401
                self.send_response(code)
                self.end_headers()
                return
            repo = q.get("repo", "")
            level = fake.levels.get(repo.lower(), fake.levels.get("", "on"))
            body = json.dumps(
                {
                    "domain": q.get("domain"),
                    "effective_level": level,
                    "resolved_scope": "repo" if repo.lower() in fake.levels else "tenant",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    fake.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=fake.server.serve_forever, daemon=True).start()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


@pytest.fixture
def real_reader(coord: FakeCoord, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeCoord:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COORD_HTTP_URL", coord.url)
    monkeypatch.setenv("COORD_DEVICE_JWT", TOKEN)
    monkeypatch.setattr(imagegen_policy, "default_reader", imagegen_policy.CoordDialReader)
    return coord


def _run(studio_root: Path, *args: str) -> Any:
    return CliRunner().invoke(main, ["--root", str(studio_root), *args])


@pytest.mark.local_http
def test_cli_brief_run_and_jobs_run_refuse_when_the_repo_row_is_off(
    studio_root: Path,
    write_brief: Callable[..., Path],
    make_context: Callable[..., Context],
    real_reader: FakeCoord,
) -> None:
    _repo(studio_root, "cafe", "acme/fixture")
    real_reader.levels["acme/fixture"] = "off"  # a REPO-band row; the tenant stays on

    r = _run(studio_root, "brief", "run", "--no-publish", str(write_brief()))
    assert r.exit_code == 6, r.output
    assert "refused: image_generation_off" in r.output and "acme/fixture" in r.output
    assert {
        "path": "/coord/fleet-policy",
        "query": {"domain": "image_generation", "repo": "acme/fixture"},
        "auth": f"Bearer {TOKEN}",
    } in real_reader.requests

    # A batch left generating (e.g. deferred to tomorrow's quota) is refused by
    # `jobs run` too, and stays generating.
    ctx = make_context()
    batch = new_batch(ctx, load_brief(write_brief()), publish=False)
    r = _run(studio_root, "jobs", "run", "--no-publish")
    assert r.exit_code == 6, r.output
    assert f"batch {batch}: generating" in r.output and "image_generation_off" in r.output
    assert _status(ctx, batch) == "generating" and _attempts(ctx) == 0

    # Clearing the override (coord's `inherit`) resumes it.
    del real_reader.levels["acme/fixture"]
    r = _run(studio_root, "jobs", "run", "--no-publish")
    assert r.exit_code == 0, r.output
    assert "image generation: on" in r.output and "[coord" in r.output
    assert _status(ctx, batch) == "generated"


@pytest.mark.local_http
def test_cli_an_unreachable_coord_uses_the_last_known_level_then_local_only(
    studio_root: Path, write_brief: Callable[..., Path], real_reader: FakeCoord
) -> None:
    real_reader.levels[""] = "off"
    r = _run(studio_root, "brief", "run", "--no-publish", str(write_brief()))
    assert r.exit_code == 6, r.output

    # coord now fails: the level it last answered (off) still governs.
    real_reader.status = 503
    r = _run(studio_root, "brief", "run", "--no-publish", str(write_brief()))
    assert r.exit_code == 6, r.output
    assert "last_known" in r.output and "HTTP 503" in r.output

    # A studio that never read the dial: local_only. The fake adapter calls
    # nothing off the machine, so it runs; the brief is not refused.
    (studio_root / ".studio" / "studio.db").unlink()
    r = _run(studio_root, "brief", "run", "--no-publish", str(write_brief()))
    assert r.exit_code == 0, r.output
    assert "image generation: local_only (tenant-wide) [unreadable]" in r.output


@pytest.mark.local_http
def test_cli_machine_switch_refuses_without_asking_coord(
    studio_root: Path,
    write_brief: Callable[..., Path],
    real_reader: FakeCoord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QONTINUI_IMAGEGEN", "0")
    r = _run(studio_root, "brief", "run", "--no-publish", str(write_brief()))
    assert r.exit_code == 6, r.output
    assert "image_generation_off" in r.output and "QONTINUI_IMAGEGEN=0" in r.output
    assert real_reader.requests == []


def test_adapter_set_route_is_none_without_a_gateway(studio_root: Path) -> None:
    inv = load_providers(studio_root / "providers.yaml")
    assert build_adapters(inv).route is None
    assert AdapterSet().route is None
