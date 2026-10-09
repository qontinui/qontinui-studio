from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from qontinui_studio import pipeline
from qontinui_studio.adapters import AdapterSet, GenerationRequest
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.errors import ConfigError, PermanentProviderError
from qontinui_studio.pipeline import Context, drive_batch, new_batch, resume_all
from qontinui_studio.scheduler import RetryPolicy
from qontinui_studio.store import connect

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
        r["provider"] for r in ctx.conn.execute("SELECT provider FROM jobs WHERE status='ingested'")
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


class NeverFinishes(FakeImageAdapter):
    def poll(self, job):  # type: ignore[no-untyped-def]
        from qontinui_studio.adapters import PollResult

        return PollResult(status="pending")


def test_poll_deadline_moves_the_job_to_the_next_provider(
    make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    ctx = make_context(
        adapters=AdapterSet(
            adapters={"fake": NeverFinishes("fake"), "fake2": FakeImageAdapter("fake2", "fake-v2")}
        ),
        policy=RetryPolicy(max_polls=3, poll_interval_s=5),
    )
    out = drive_batch(
        ctx, new_batch(ctx, load_brief(write_brief(providers="fake, fake2"))), publish=False
    )
    assert out.status == "generated"
    row = ctx.conn.execute("SELECT * FROM jobs").fetchone()
    assert row["provider"] == "fake2"
    assert json.loads(row["tried_providers"]) == ["fake"]


def test_quota_and_state_are_committed_before_the_request_leaves(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    seen: dict[str, int] = {}

    class Spy(FakeImageAdapter):
        def submit(self, request, staging_dir):  # type: ignore[no-untyped-def]
            # A second connection sees the quota spend: it is already durable.
            other = connect(studio_root / ".studio" / "studio.db")
            seen["used"] = other.execute("SELECT used FROM quota_usage").fetchone()["used"]
            other.close()
            return super().submit(request, staging_dir)

    ctx = make_context(adapters=AdapterSet(adapters={"fake": Spy("fake")}))
    drive_batch(ctx, new_batch(ctx, load_brief(write_brief())), publish=False)
    assert seen == {"used": 1}


def test_crash_between_download_and_ingest_is_repaired_on_resume(
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_context()
    batch = new_batch(ctx, load_brief(write_brief()))
    real_export = pipeline.export

    def crash(*args: object, **kwargs: object) -> Path:
        raise OSError("killed mid-ingest")

    monkeypatch.setattr(pipeline, "export", crash)
    with pytest.raises(OSError):
        drive_batch(ctx, batch, publish=False)
    row = ctx.conn.execute("SELECT status FROM jobs").fetchone()
    assert row["status"] == "downloaded"
    assert ctx.conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"] == 0

    monkeypatch.setattr(pipeline, "export", real_export)
    out = drive_batch(make_context(), batch, publish=True)
    assert out.status == "published"
    assert ctx.conn.execute("SELECT status FROM jobs").fetchone()["status"] == "ingested"


def test_changed_brief_refuses_its_batch_and_resume_isolates_it(
    make_context: Callable[..., Context], write_brief: Callable[..., Path], studio_root: Path
) -> None:
    _set_quota(studio_root, "fake", 1)
    ctx = make_context()
    changed = write_brief(variants=2, name="a.yaml")
    healthy = write_brief(variants=1, name="b.yaml")
    b1 = new_batch(ctx, load_brief(changed))
    out1 = drive_batch(ctx, b1, publish=False)
    assert out1.status == "generating"  # one variant deferred to tomorrow
    changed.write_text(changed.read_text() + "\n# edited\n")
    ctx.conn.execute("UPDATE quota_usage SET used = 0")
    ctx.conn.commit()
    b2 = new_batch(ctx, load_brief(healthy))
    with pytest.raises(ConfigError, match="changed since batch"):
        drive_batch(ctx, b1, publish=False)
    assert (
        ctx.conn.execute("SELECT status FROM batches WHERE id = ?", (b1,)).fetchone()[0] == "failed"
    )

    # resume_all: the bad batch reports an error, the good one still runs.
    ctx.conn.execute("UPDATE batches SET status = 'generating' WHERE id = ?", (b1,))
    ctx.conn.commit()
    outcomes = {o.batch_id: o for o in resume_all(ctx, publish=False)}
    assert outcomes[b1].error and "changed since batch" in outcomes[b1].error
    assert outcomes[b2].status == "generated"
