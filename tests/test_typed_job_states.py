"""Typed job states in the scheduler, unmetered local capacity, and the
fallback rules (plan 2026-10-13 Phase 11 steps 1, 2 and 5).

* ``deferred``/``busy`` requeue at the host's ``retry_after_s`` without
  spending a quota unit or an attempt; a ``deferred``, or a ``busy`` answered
  to a bulk job, never moves the job to another provider.
* ``unknown`` is recorded as itself — never ``failed`` or ``done`` — and the
  job is retried.
* An interactive job may fall back after ``busy``/``unknown``, but only to a
  provider with capacity (a cloud adapter only within its own quota).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from qontinui_studio.adapters import AdapterSet, GenerationRequest, PollResult, SubmittedJob
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.errors import (
    ConfigError,
    ProviderBusy,
    ProviderDeferred,
    ProviderOutcomeUnknown,
)
from qontinui_studio.pipeline import Context, _request_for, drive_batch, new_batch
from qontinui_studio.providers import is_local_network, load_providers
from qontinui_studio.scheduler import RetryPolicy

from .conftest import FakeClock

LOCAL_ROW = """
  - id: local-gpu
    provider: Local image facade
    plan: self-hosted
    api_available: {{value: "yes", source: "https://local.test/api"}}
    automation_allowed: {{value: "yes", source: "https://local.test/api"}}
    commercial_use: {{value: "yes", source: "https://local.test/licence"}}
    watermark: IPTC trainedAlgorithmicMedia (XMP)
    watermark_visible: false
    adapter: openai_compatible_images
    model: z-image-turbo
    base_url: {base}
    capacity: unmetered
    tos_reviewed: 2026-10-15
    domains: [local.test]
"""


class Scripted(FakeImageAdapter):
    """A fake whose submits first raise the scripted errors, in order."""

    def __init__(self, pid: str, model: str, *script: Exception) -> None:
        super().__init__(pid, model)
        self.script = list(script)
        self.calls = 0

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        self.calls += 1
        if self.script:
            raise self.script.pop(0)
        return super().submit(request, staging_dir)


class PollsOnce(FakeImageAdapter):
    """An asynchronous fake whose first poll answers ``status``."""

    def __init__(self, pid: str, model: str, status: str) -> None:
        super().__init__(pid, model)
        self.first = status
        self.submits = 0

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        self.submits += 1
        return super().submit(request, staging_dir)

    def poll(self, job: SubmittedJob) -> PollResult:
        if self.first:
            status, self.first = self.first, ""
            return PollResult(status=status)  # type: ignore[arg-type]
        return PollResult(status="done")


def _setup(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    adapters: dict[str, FakeImageAdapter],
    providers: str = "fake, fake2",
    priority: str = "interactive",
) -> tuple[Context, str, dict[str, tuple[list[str], GenerationRequest]]]:
    path = write_brief(providers=providers)
    path.write_text(path.read_text() + f"priority: {priority}\n")
    loaded = load_brief(path)
    ctx = make_context(adapters=AdapterSet(adapters=dict(adapters)))
    batch = new_batch(ctx, loaded)
    return ctx, batch, {batch: _request_for(loaded)}


def _job(ctx: Context) -> dict[str, object]:
    return dict(ctx.conn.execute("SELECT * FROM jobs").fetchone())


def _ledger(ctx: Context) -> list[tuple[str, str]]:
    return [
        (r["provider"], r["outcome"])
        for r in ctx.conn.execute("SELECT provider, outcome FROM job_attempts ORDER BY id")
    ]


def _used(ctx: Context, provider: str) -> int:
    row = ctx.conn.execute(
        "SELECT COALESCE(SUM(used), 0) AS u FROM quota_usage WHERE provider = ?", (provider,)
    ).fetchone()
    return int(row["u"])


@pytest.mark.parametrize("priority", ["bulk", "interactive"])
@pytest.mark.parametrize("refusal", ["busy", "deferred"])
def test_a_refusal_requeues_at_retry_after_without_spending_quota_or_an_attempt(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
    refusal: str,
    priority: str,
) -> None:
    exc = (ProviderBusy if refusal == "busy" else ProviderDeferred)(f"{refusal}", 120.0)
    fake = Scripted("fake", "fake-v1", exc)
    fake2 = Scripted("fake2", "fake-v2")
    ctx, batch, req = _setup(
        make_context, write_brief, {"fake": fake, "fake2": fake2}, priority=priority
    )
    ctx.scheduler().step(req)
    job = _job(ctx)
    moved = refusal == "busy" and priority == "interactive"
    if moved:
        # The one case that may move: an interactive busy, to a provider with quota.
        assert job["provider"] == "fake2" and job["next_attempt_at"] == clock.now.isoformat()
    else:
        assert job["provider"] == "fake"
        assert job["next_attempt_at"] == (clock.now + timedelta(seconds=120)).isoformat()
    assert job["status"] == "queued" and job["attempts"] == 0
    assert _used(ctx, "fake") == 0, "the refused submission's unit is refunded"
    assert json.loads(str(job["tried_providers"])) == []
    assert _ledger(ctx) == [("fake", refusal)]
    assert fake2.calls == 0

    clock.sleep(120)
    ctx.scheduler().step(req)
    assert _job(ctx)["status"] == "downloaded"
    winner = "fake2" if moved else "fake"
    assert _ledger(ctx) == [("fake", refusal), (winner, "done")]
    assert _used(ctx, winner) == 1


def test_an_interactive_busy_stays_put_when_no_fallback_has_quota(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
) -> None:
    p = studio_root / "providers.yaml"
    head, _, tail = p.read_text().rpartition("daily_quota: 10")  # fake2's row
    p.write_text(head + "daily_quota: null" + tail)
    fake = Scripted("fake", "fake-v1", ProviderBusy("busy", 60.0))
    fake2 = Scripted("fake2", "fake-v2")
    ctx, _, req = _setup(make_context, write_brief, {"fake": fake, "fake2": fake2})
    assert ctx.inventory.get("fake2").daily_quota is None
    ctx.scheduler().step(req)
    job = _job(ctx)
    assert job["provider"] == "fake" and fake2.calls == 0
    assert job["next_attempt_at"] == (clock.now + timedelta(seconds=60)).isoformat()


def test_unknown_is_recorded_as_unknown_and_the_job_is_retried(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    fake = Scripted("fake", "fake-v1", ProviderOutcomeUnknown("dropped mid-flight"))
    ctx, _, req = _setup(
        make_context, write_brief, {"fake": fake}, providers="fake", priority="bulk"
    )
    ctx.scheduler().step(req)
    job = _job(ctx)
    assert job["status"] == "queued", "unknown is never collapsed into failed"
    assert job["attempts"] == 1 and "unknown" in str(job["last_error"])
    # The provider may have done the work: its unit stays spent.
    assert _used(ctx, "fake") == 1
    assert _ledger(ctx) == [("fake", "unknown")]
    clock.sleep(3600)
    ctx.scheduler().step(req)
    assert _job(ctx)["status"] == "downloaded"
    assert _ledger(ctx) == [("fake", "unknown"), ("fake", "done")]


def test_an_interactive_unknown_may_fall_back(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    fake = Scripted("fake", "fake-v1", ProviderOutcomeUnknown("dropped"))
    fake2 = Scripted("fake2", "fake-v2")
    ctx, _, req = _setup(make_context, write_brief, {"fake": fake, "fake2": fake2})
    ctx.scheduler().step(req)
    assert _job(ctx)["provider"] == "fake2"
    ctx.scheduler().step(req)
    assert _ledger(ctx) == [("fake", "unknown"), ("fake2", "done")]


def test_repeated_unknowns_give_up_on_the_provider_but_never_record_failed(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    fake = Scripted("fake", "fake-v1", *[ProviderOutcomeUnknown("dropped") for _ in range(4)])
    ctx, _, req = _setup(
        make_context, write_brief, {"fake": fake}, providers="fake", priority="bulk"
    )
    ctx.policy = RetryPolicy(max_attempts_per_provider=2, base_backoff_s=1)
    for _ in range(4):
        ctx.scheduler().step(req)
        clock.sleep(10)
    job = _job(ctx)
    assert job["status"] == "failed"
    assert "unknown" in str(job["last_error"])
    assert {o for _, o in _ledger(ctx)} == {"unknown"}


@pytest.mark.parametrize("status", ["unknown", "pending"])
def test_a_poll_that_loses_the_job_is_unknown_and_resubmits(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
    status: str,
) -> None:
    """A poll answering ``unknown`` (or a status the scheduler does not know)
    is never read as done: the job is resubmitted."""
    fake = PollsOnce("fake", "fake-v1", status)
    ctx, _, req = _setup(
        make_context, write_brief, {"fake": fake}, providers="fake", priority="bulk"
    )
    ctx.scheduler().step(req)
    assert _job(ctx)["status"] == "queued"
    assert _ledger(ctx) == [("fake", "unknown")]
    clock.sleep(3600)
    ctx.scheduler().step(req)
    assert fake.submits == 2 and _job(ctx)["status"] == "downloaded"


@pytest.mark.parametrize("status", ["busy", "deferred"])
def test_a_poll_time_refusal_refunds_and_waits(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    status: str,
) -> None:
    fake = PollsOnce("fake", "fake-v1", status)
    ctx, _, req = _setup(
        make_context, write_brief, {"fake": fake}, providers="fake", priority="bulk"
    )
    ctx.scheduler().step(req)
    job = _job(ctx)
    assert job["status"] == "queued" and job["provider"] == "fake" and job["attempts"] == 0
    assert _used(ctx, "fake") == 0
    assert _ledger(ctx) == [("fake", status)]


def test_a_stale_submission_reclaim_is_recorded_as_unknown(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    ctx, _, req = _setup(
        make_context, write_brief, {"fake": FakeImageAdapter("fake")}, providers="fake"
    )
    ctx.conn.execute(
        "UPDATE jobs SET status = 'submitting', provider = 'fake', claim = 'gone', "
        "next_attempt_at = ?",
        ((clock.now - timedelta(seconds=1)).isoformat(),),
    )
    ctx.conn.commit()
    ctx.scheduler().step(req)
    assert _ledger(ctx)[0] == ("fake", "unknown")


# ---- unmetered local capacity ---------------------------------------------------


def _with_local(studio_root: Path, base: str) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + LOCAL_ROW.format(base=base))


def test_an_unmetered_local_provider_is_scheduled_without_a_quota(
    studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    _with_local(studio_root, "http://127.0.0.1:8100/v1")
    local = FakeImageAdapter("local-gpu", "z-image-turbo")
    ctx, _, req = _setup(make_context, write_brief, {"local-gpu": local}, providers="local-gpu")
    assert ctx.inventory.get("local-gpu").schedulable_reason() is None
    ctx.scheduler().step(req)
    job = _job(ctx)
    assert job["status"] == "downloaded" and job["provider"] == "local-gpu"
    assert job["quota_day"] is None
    assert ctx.conn.execute("SELECT COUNT(*) FROM quota_usage").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://127.0.0.1:8100/v1", True),
        ("http://localhost:8100/v1", True),
        ("http://spaceship:8100/v1", True),
        ("http://spaceship.tail1234.ts.net:8100/v1", True),
        ("http://100.111.28.125:8100/v1", True),
        ("http://192.168.1.20:8100/v1", True),
        ("http://[::1]:8100/v1", True),
        ("https://api.openai.com/v1", False),
        ("http://8.8.8.8/v1", False),
        ("not a url", False),
    ],
)
def test_unmetered_needs_a_local_network_host(url: str, local: bool) -> None:
    assert is_local_network(url) is local


def test_an_unmetered_provider_on_a_public_host_is_not_schedulable(
    studio_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(
        p.read_text()
        + LOCAL_ROW.format(base="http://127.0.0.1:8100/v1")
        + "    base_url_env: STUDIO_TEST_LOCAL_BASE\n"
    )
    monkeypatch.setenv("STUDIO_TEST_LOCAL_BASE", "https://images.example.com/v1")
    reason = load_providers(p).get("local-gpu").schedulable_reason()
    assert reason is not None and "local-network" in reason


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (("capacity: unmetered", "capacity: unmetered\n    daily_quota: 5"), "no daily_quota"),
        (("adapter: openai_compatible_images", "adapter: openai_images"), "self-hosted"),
        (("    base_url: http://127.0.0.1:8100/v1\n", ""), "needs a base_url"),
    ],
)
def test_unmetered_rows_are_validated(
    studio_root: Path, change: tuple[str, str], match: str
) -> None:
    _with_local(studio_root, "http://127.0.0.1:8100/v1")
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text().replace(*change))
    with pytest.raises(ConfigError, match=match):
        load_providers(p)


def test_bulk_work_refused_by_the_local_host_never_spills_onto_a_paid_adapter(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    clock: FakeClock,
) -> None:
    """The money property end to end: local first, a metered fake with quota
    second, the host answering ``busy`` to a bulk job — the job waits for the
    host, and the metered provider is never called."""
    _with_local(studio_root, "http://127.0.0.1:8100/v1")
    local = Scripted("local-gpu", "z", *[ProviderBusy("busy", 300.0) for _ in range(3)])
    paid = Scripted("fake", "fake-v1")
    ctx, batch, req = _setup(
        make_context,
        write_brief,
        {"local-gpu": local, "fake": paid},
        providers="local-gpu, fake",
        priority="bulk",
    )
    out = drive_batch(ctx, batch, publish=False)
    assert paid.calls == 0 and local.calls >= 3
    assert out.status in ("generating", "generated")
    assert {p for p, _ in _ledger(ctx)} == {"local-gpu"}
