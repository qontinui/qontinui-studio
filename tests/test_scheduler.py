from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qontinui_studio.adapters import AdapterSet, GenerationRequest
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.errors import PermanentProviderError
from qontinui_studio.pipeline import Context, drive_batch, new_batch
from qontinui_studio.scheduler import RetryPolicy

from .conftest import FakeClock


def _set_quota(studio_root: Path, provider: str, quota: int) -> None:
    p = studio_root / "providers.yaml"
    text = p.read_text()
    marker = f"  - id: {provider}\n"
    head, tail = text.split(marker)
    tail = tail.replace("daily_quota: 10", f"daily_quota: {quota}", 1)
    p.write_text(head + marker + tail)


def test_jobs_spread_across_quotas_and_defer_to_tomorrow(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    studio_root: Path,
    clock: FakeClock,
) -> None:
    _set_quota(studio_root, "fake", 1)
    _set_quota(studio_root, "fake2", 1)
    ctx = make_context()
    loaded = load_brief(write_brief(providers="fake, fake2", variants=3))
    batch = new_batch(ctx, loaded)
    out = drive_batch(ctx, batch, publish=False)
    assert out.status == "generating"
    assert len(out.completed_jobs) == 2 and len(out.deferred_jobs) == 1
    providers = {
        r["provider"] for r in ctx.conn.execute("SELECT provider FROM jobs WHERE status='done'")
    }
    assert providers == {"fake", "fake2"}
    deferred = ctx.conn.execute("SELECT * FROM jobs WHERE status='queued'").fetchone()
    assert deferred["next_attempt_at"].startswith("2026-10-10T00:00:00")

    # Next UTC day, a FRESH process (new context, new scheduler) resumes from SQLite.
    clock.sleep(13 * 3600)
    ctx2 = make_context()
    out2 = drive_batch(ctx2, batch, publish=False)
    assert out2.status == "generated"
    assert len(out2.completed_jobs) == 3


def test_transient_failures_back_off_then_succeed(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], clock: FakeClock
) -> None:
    flaky = FakeImageAdapter("fake", "fake-v1", fail_first=2)
    ctx = make_context(adapters=AdapterSet(adapters={"fake": flaky}))
    start = clock.now
    loaded = load_brief(write_brief())
    out = drive_batch(ctx, new_batch(ctx, loaded), publish=False)
    assert out.status == "generated"
    # 30s then 60s backoff before the third (successful) attempt, plus one poll interval.
    assert (clock.now - start).total_seconds() >= 90
    used = ctx.conn.execute("SELECT used FROM quota_usage WHERE provider='fake'").fetchone()
    assert used["used"] == 3  # every request to the provider counts against its quota


class AlwaysRefuses(FakeImageAdapter):
    def submit(self, request: GenerationRequest, staging_dir: Path):  # type: ignore[no-untyped-def]
        raise PermanentProviderError("content policy")


def test_permanent_failure_moves_to_next_preferred_provider(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context(
        adapters=AdapterSet(
            adapters={"fake": AlwaysRefuses("fake"), "fake2": FakeImageAdapter("fake2", "fake-v2")}
        )
    )
    out = drive_batch(
        ctx, new_batch(ctx, load_brief(write_brief(providers="fake, fake2"))), publish=False
    )
    assert out.status == "generated"
    row = ctx.conn.execute("SELECT * FROM jobs").fetchone()
    assert row["provider"] == "fake2"


def test_exhausting_every_provider_fails_the_job(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context(
        adapters=AdapterSet(adapters={"fake": AlwaysRefuses("fake")}),
        policy=RetryPolicy(max_attempts_per_provider=2),
    )
    out = drive_batch(ctx, new_batch(ctx, load_brief(write_brief())), publish=False)
    assert out.status == "failed"
    row = ctx.conn.execute("SELECT * FROM jobs").fetchone()
    assert row["status"] == "failed" and "content policy" in row["last_error"]


def test_retry_policy_backoff() -> None:
    p = RetryPolicy(base_backoff_s=10, max_backoff_s=60)
    assert [p.backoff(n, None) for n in (1, 2, 3, 4, 5)] == [10, 20, 40, 60, 60]
    assert p.backoff(1, 45.0) == 45.0
