"""Job scheduler: spreads generation across each provider's daily free quota.

State is persisted in SQLite, so a crashed or quota-deferred run resumes where
it stopped. Per job the scheduler walks the brief's provider preference,
skipping any provider that is unschedulable (D4 / no free quota), whose adapter
could not be built, or whose quota for the current UTC day is spent. A
transient failure is retried with exponential backoff; once a provider has
exhausted its attempts the job moves to the next preferred provider. When every
preferred provider is spent for today, the job is deferred to the next UTC
midnight rather than failed.

Concurrency: every step that acts on a job first CLAIMS it with a fresh token
(``jobs.claim``) in a conditional update, and every later write is conditional
on that token and the expected status. A process whose claim was taken over —
because it outlived its lease — has its late result discarded, never merged
with another process's state. A submission's lease is longer than the adapter's
hard timeout, so a live request is never reclaimed.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from qontinui_studio.adapters import AdapterSet, GenerationRequest, SubmittedJob
from qontinui_studio.errors import PermanentProviderError, TransientProviderError
from qontinui_studio.library import TermsSnapshot
from qontinui_studio.providers import ProviderInventory

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_provider: int = 4
    base_backoff_s: float = 30.0
    max_backoff_s: float = 3600.0
    poll_interval_s: float = 5.0
    # A submitted job that is still pending after either cap is abandoned on
    # that provider and moves to the next preferred one.
    max_polls: int = 720
    poll_timeout_s: float = 3600.0
    # Minimum lease of a 'submitting' claim. The effective lease is the larger
    # of this and the adapter's hard timeout + 60 s, so a request still in
    # flight is never reclaimed.
    submit_stale_s: float = 900.0
    # Lease of one poll/download round.
    poll_lease_s: float = 900.0

    def backoff(self, attempts: int, retry_after_s: float | None) -> float:
        delay: float = min(self.max_backoff_s, self.base_backoff_s * 2.0 ** max(0, attempts - 1))
        if retry_after_s is not None:
            delay = max(delay, retry_after_s)
        return delay


@dataclass
class CompletedJob:
    job_id: str
    batch_id: str
    variant_index: int
    provider: str
    model: str
    files: list[Path]


@dataclass
class StepReport:
    completed: list[CompletedJob] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        inventory: ProviderInventory,
        adapters: AdapterSet,
        staging_dir: Path,
        raw_dir: Path,
        clock: Clock = utc_now,
        policy: RetryPolicy | None = None,
    ) -> None:
        self.conn = conn
        self.inventory = inventory
        self.adapters = adapters
        self.staging_dir = staging_dir
        self.raw_dir = raw_dir
        self.clock = clock
        self.policy = policy or RetryPolicy()

    # ---- quota -----------------------------------------------------------

    def quota_used(self, provider_id: str, day: str) -> int:
        row = self.conn.execute(
            "SELECT used FROM quota_usage WHERE provider = ? AND day = ?", (provider_id, day)
        ).fetchone()
        return int(row["used"]) if row else 0

    def quota_remaining(self, provider_id: str, now: datetime) -> int:
        provider = self.inventory.get(provider_id)
        if provider.schedulable_reason() is not None or provider.daily_quota is None:
            return 0
        return max(0, provider.daily_quota - self.quota_used(provider_id, now.date().isoformat()))

    def _consume_quota(self, provider_id: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO quota_usage (provider, day, used) VALUES (?, ?, 1) "
            "ON CONFLICT(provider, day) DO UPDATE SET used = used + 1",
            (provider_id, now.date().isoformat()),
        )

    # ---- enqueue ---------------------------------------------------------

    def enqueue(self, batch_id: str, variants: int) -> list[str]:
        now = self.clock()
        ids = []
        for i in range(variants):
            job_id = f"{batch_id}-v{i}"
            self.conn.execute(
                "INSERT INTO jobs (id, batch_id, variant_index, status, next_attempt_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (job_id, batch_id, i, _iso(now), _iso(now)),
            )
            ids.append(job_id)
        self.conn.commit()
        return ids

    # ---- conditional writes ------------------------------------------------

    def _write(self, job_id: str, claim: str | None, status: str, /, **fields: object) -> bool:
        """Update a job only if it is still in ``status`` under ``claim``.
        Returns False (and changes nothing) when another process took it over."""
        fields["updated_at"] = _iso(self.clock())
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE jobs SET {cols} WHERE id = ? AND status = ? AND claim IS ?",
            (*fields.values(), job_id, status, claim),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def _lease(self, adapter_timeout_s: float, floor_s: float) -> float:
        return max(floor_s, adapter_timeout_s + 60.0)

    # ---- the step --------------------------------------------------------

    def _pick_provider(self, preference: list[str], tried: list[str], now: datetime) -> str | None:
        for pid in preference:
            if pid in tried or pid not in self.adapters.adapters:
                continue
            if self.quota_remaining(pid, now) > 0:
                return pid
        return None

    def _any_provider_left_later(self, preference: list[str], tried: list[str]) -> bool:
        """True when some untried preferred provider has an adapter and a quota —
        i.e. it is only today's quota that is spent."""
        for pid in preference:
            if pid in tried or pid not in self.adapters.adapters:
                continue
            if self.inventory.get(pid).schedulable_reason() is None:
                return True
        return False

    def step(self, requests: dict[str, tuple[list[str], GenerationRequest]]) -> StepReport:
        """Advance every due job once. ``requests`` maps batch id ->
        (provider preference, request template)."""
        report = StepReport()
        now = self.clock()
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'submitting', 'submitted') "
            "AND next_attempt_at <= ? ORDER BY next_attempt_at, id",
            (_iso(now),),
        ).fetchall()
        for row in rows:
            batch_id = row["batch_id"]
            if batch_id not in requests:
                continue
            preference, template = requests[batch_id]
            if row["status"] == "submitting":
                # The claim outlived its lease (longer than the adapter's hard
                # timeout): its process is gone. Requeue; if that process ever
                # returns, its writes no longer match the claim and are dropped.
                if not self._write(
                    row["id"],
                    row["claim"],
                    "submitting",
                    status="queued",
                    claim=None,
                    next_attempt_at=_iso(now),
                    last_error="reclaimed a stale submission (outcome unknown)",
                ):
                    continue
                row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            if row["status"] == "queued":
                self._submit(row, preference, template, now, report)
            elif row["status"] == "submitted":
                self._poll(row, now, report)
        self.conn.commit()
        return report

    def _submit(
        self,
        row: sqlite3.Row,
        preference: list[str],
        template: GenerationRequest,
        now: datetime,
        report: StepReport,
    ) -> None:
        tried: list[str] = json.loads(row["tried_providers"])
        provider_id = row["provider"]
        if (
            provider_id is None
            or provider_id in tried
            or provider_id not in self.adapters.adapters  # not built this run
            or self.quota_remaining(provider_id, now) <= 0
        ):
            provider_id = self._pick_provider(preference, tried, now)
        if provider_id is None:
            if self._any_provider_left_later(preference, tried):
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                self._write(
                    row["id"],
                    row["claim"],
                    "queued",
                    provider=None,
                    next_attempt_at=_iso(tomorrow),
                    last_error="every preferred provider's daily quota is spent; deferred",
                )
            elif self._write(
                row["id"],
                row["claim"],
                "queued",
                status="failed",
                last_error=row["last_error"] or "no schedulable provider in the brief's preference",
            ):
                report.failed.append(row["id"])
            return

        attempts = row["attempts"] if provider_id == row["provider"] else 0
        adapter = self.adapters.adapters[provider_id]
        token = uuid.uuid4().hex
        request = GenerationRequest(
            job_id=row["id"],
            prompt=template.prompt,
            aspect_ratio=template.aspect_ratio,
            reference_images=template.reference_images,
        )
        lease = self._lease(adapter.hard_timeout_s, self.policy.submit_stale_s)
        # Atomic claim. It records exactly what is being sent (prompt, model)
        # and the terms as reviewed right now (D5); ingest reads only these.
        # Quota is spent in the same transaction, BEFORE the request leaves.
        claimed = self.conn.execute(
            "UPDATE jobs SET status = 'submitting', claim = ?, provider = ?, prompt = ?, "
            "model = ?, terms_json = ?, next_attempt_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'queued' AND claim IS ?",
            (
                token,
                provider_id,
                request.prompt,
                adapter.model,
                TermsSnapshot.of(self.inventory.get(provider_id)).to_json(),
                _iso(now + timedelta(seconds=lease)),
                _iso(now),
                row["id"],
                row["claim"],
            ),
        ).rowcount
        if claimed != 1:
            self.conn.rollback()
            return  # another process claimed it
        self._consume_quota(provider_id, now)
        self.conn.commit()

        try:
            handle = adapter.submit(request, self.staging_dir / row["id"] / token)
        except TransientProviderError as e:
            attempts += 1
            if attempts >= self.policy.max_attempts_per_provider:
                self._give_up_on_provider(
                    row["id"], token, "submitting", provider_id, tried, str(e), now
                )
            else:
                delay = self.policy.backoff(attempts, e.retry_after_s)
                self._write(
                    row["id"],
                    token,
                    "submitting",
                    status="queued",
                    claim=None,
                    attempts=attempts,
                    next_attempt_at=_iso(now + timedelta(seconds=delay)),
                    last_error=str(e),
                )
            return
        except PermanentProviderError as e:
            self._give_up_on_provider(
                row["id"], token, "submitting", provider_id, tried, str(e), now
            )
            return
        # Persist the handle at once, under our claim. If the claim was taken
        # over while we waited on the provider, this result is discarded.
        if not self._write(
            row["id"],
            token,
            "submitting",
            status="submitted",
            attempts=attempts + 1,
            submitted_at=_iso(now),
            polls=0,
            external_id=handle.external_id,
            handle_json=json.dumps(handle.data),
            next_attempt_at=_iso(now),
            last_error=None,
        ):
            report.discarded.append(row["id"])
            return
        # Poll straight away; a synchronous provider is already done.
        fresh = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        self._poll(fresh, now, report)

    def _give_up_on_provider(
        self,
        job_id: str,
        claim: str | None,
        status: str,
        provider_id: str,
        tried: list[str],
        error: str,
        now: datetime,
    ) -> bool:
        return self._write(
            job_id,
            claim,
            status,
            provider=None,
            claim=None,
            attempts=0,
            polls=0,
            submitted_at=None,
            tried_providers=json.dumps([*tried, provider_id]),
            next_attempt_at=_iso(now),
            last_error=f"{provider_id}: {error}",
            status="queued",
        )

    def _poll(self, row: sqlite3.Row, now: datetime, report: StepReport) -> None:
        provider_id = row["provider"]
        tried: list[str] = json.loads(row["tried_providers"])
        adapter = self.adapters.adapters.get(provider_id)
        if adapter is None:
            # Not built this run (key unset, D4 now off): move on rather than fail.
            self._give_up_on_provider(
                row["id"],
                row["claim"],
                "submitted",
                provider_id,
                tried,
                "adapter unavailable this run",
                now,
            )
            return
        # Claim this poll round, so two processes never poll/download one job.
        token = uuid.uuid4().hex
        lease = self._lease(adapter.hard_timeout_s, self.policy.poll_lease_s)
        if not self._write(
            row["id"],
            row["claim"],
            "submitted",
            claim=token,
            next_attempt_at=_iso(now + timedelta(seconds=lease)),
        ):
            return
        job_id = row["id"]

        def release(**fields: object) -> bool:
            return self._write(job_id, token, "submitted", **fields)

        def give_up(error: str) -> None:
            self._give_up_on_provider(job_id, token, "submitted", provider_id, tried, error, now)

        handle = SubmittedJob(external_id=row["external_id"], data=json.loads(row["handle_json"]))
        polls = int(row["polls"]) + 1
        submitted_at = _parse(row["submitted_at"]) if row["submitted_at"] else now
        overdue = (
            polls > self.policy.max_polls
            or (now - submitted_at).total_seconds() > self.policy.poll_timeout_s
        )
        next_poll = _iso(now + timedelta(seconds=self.policy.poll_interval_s))
        try:
            result = adapter.poll(handle)
        except PermanentProviderError as e:
            give_up(str(e))
            return
        except TransientProviderError as e:
            if overdue:
                give_up(f"poll deadline passed: {e}")
            else:
                release(polls=polls, next_attempt_at=next_poll, last_error=str(e))
            return
        if result.status == "pending":
            if overdue:
                give_up(
                    f"still pending after {polls} polls / "
                    f"{(now - submitted_at).total_seconds():.0f}s"
                )
            else:
                release(polls=polls, next_attempt_at=next_poll)
            return
        if result.status == "failed":
            give_up(result.error or "provider job failed")
            return
        try:
            files = adapter.download(handle, self.raw_dir / row["batch_id"] / f"{job_id}-{token}")
        except TransientProviderError as e:
            # Download retries count against the same poll cap / deadline.
            if overdue:
                give_up(f"download deadline passed: {e}")
            else:
                release(polls=polls, next_attempt_at=next_poll, last_error=f"download: {e}")
            return
        except PermanentProviderError as e:
            give_up(f"download: {e}")
            return
        if not files:
            give_up("download returned no files")
            return
        if not release(
            status="downloaded",
            polls=polls,
            files_json=json.dumps([str(f) for f in files]),
            last_error=None,
        ):
            report.discarded.append(job_id)
            return
        report.completed.append(
            CompletedJob(
                job_id=job_id,
                batch_id=row["batch_id"],
                variant_index=row["variant_index"],
                provider=provider_id,
                model=adapter.model,
                files=files,
            )
        )

    # ---- driving ---------------------------------------------------------

    def pending_jobs(self, batch_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? "
            "AND status IN ('queued', 'submitting', 'submitted')",
            (batch_id,),
        ).fetchall()

    def run(
        self,
        requests: dict[str, tuple[list[str], GenerationRequest]],
        sleep: Callable[[float], None] = time.sleep,
        horizon: timedelta = timedelta(minutes=30),
    ) -> StepReport:
        """Step until every job of the given batches is terminal or deferred
        beyond ``horizon`` (e.g. to tomorrow's quota)."""
        total = StepReport()
        while True:
            r = self.step(requests)
            total.completed.extend(r.completed)
            total.failed.extend(r.failed)
            total.discarded.extend(r.discarded)
            pending = [j for b in requests for j in self.pending_jobs(b)]
            if not pending:
                return total
            now = self.clock()
            soonest = min(_parse(j["next_attempt_at"]) for j in pending)
            if soonest - now > horizon:
                return total
            wait = max(0.0, (soonest - now).total_seconds())
            if wait > 0:
                sleep(wait)
