"""Job scheduler: spreads generation across each provider's daily free quota.

State is persisted in SQLite, so a crashed or quota-deferred run resumes where
it stopped. Per job the scheduler walks the brief's provider preference,
skipping any provider that is unschedulable (D4 / no free quota), whose adapter
could not be built, or whose quota for the current UTC day is spent. A
transient failure is retried with exponential backoff; once a provider has
exhausted its attempts the job moves to the next preferred provider. When every
preferred provider is spent for today, the job is deferred to the next UTC
midnight rather than failed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from qontinui_studio.adapters import AdapterSet, GenerationRequest, SubmittedJob
from qontinui_studio.errors import PermanentProviderError, TransientProviderError
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
            "SELECT * FROM jobs WHERE status IN ('queued', 'submitted') AND next_attempt_at <= ? "
            "ORDER BY next_attempt_at, id",
            (_iso(now),),
        ).fetchall()
        for row in rows:
            batch_id = row["batch_id"]
            if batch_id not in requests:
                continue
            preference, template = requests[batch_id]
            if row["status"] == "queued":
                self._submit(row, preference, template, now, report)
            else:
                self._poll(row, now, report)
        self.conn.commit()
        return report

    def _update(self, job_id: str, **fields: object) -> None:
        fields["updated_at"] = _iso(self.clock())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

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
            or self.quota_remaining(provider_id, now) <= 0
        ):
            provider_id = self._pick_provider(preference, tried, now)
        if provider_id is None:
            if self._any_provider_left_later(preference, tried):
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                self._update(
                    row["id"],
                    provider=None,
                    next_attempt_at=_iso(tomorrow),
                    last_error="every preferred provider's daily quota is spent; deferred",
                )
            else:
                self._update(
                    row["id"],
                    status="failed",
                    last_error=row["last_error"]
                    or "no schedulable provider in the brief's preference",
                )
                report.failed.append(row["id"])
            return

        attempts = row["attempts"] if provider_id == row["provider"] else 0
        adapter = self.adapters.adapters[provider_id]
        request = GenerationRequest(
            job_id=row["id"],
            prompt=template.prompt,
            aspect_ratio=template.aspect_ratio,
            reference_images=template.reference_images,
        )
        self._consume_quota(provider_id, now)
        try:
            handle = adapter.submit(request, self.staging_dir / row["id"])
        except TransientProviderError as e:
            attempts += 1
            if attempts >= self.policy.max_attempts_per_provider:
                self._give_up_on_provider(row, provider_id, tried, str(e), now)
            else:
                delay = self.policy.backoff(attempts, e.retry_after_s)
                self._update(
                    row["id"],
                    provider=provider_id,
                    attempts=attempts,
                    next_attempt_at=_iso(now + timedelta(seconds=delay)),
                    last_error=str(e),
                )
            return
        except PermanentProviderError as e:
            self._give_up_on_provider(row, provider_id, tried, str(e), now)
            return
        self._update(
            row["id"],
            provider=provider_id,
            attempts=attempts + 1,
            status="submitted",
            external_id=handle.external_id,
            handle_json=json.dumps(handle.data),
            next_attempt_at=_iso(now),
            last_error=None,
        )
        # Poll straight away; a synchronous provider is already done.
        fresh = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        self._poll(fresh, now, report)

    def _give_up_on_provider(
        self, row: sqlite3.Row, provider_id: str, tried: list[str], error: str, now: datetime
    ) -> None:
        tried = [*tried, provider_id]
        self._update(
            row["id"],
            provider=None,
            attempts=0,
            tried_providers=json.dumps(tried),
            next_attempt_at=_iso(now),
            last_error=f"{provider_id}: {error}",
            status="queued",
        )

    def _poll(self, row: sqlite3.Row, now: datetime, report: StepReport) -> None:
        provider_id = row["provider"]
        adapter = self.adapters.adapters.get(provider_id)
        if adapter is None:
            self._update(
                row["id"], status="failed", last_error=f"{provider_id}: adapter unavailable"
            )
            report.failed.append(row["id"])
            return
        handle = SubmittedJob(external_id=row["external_id"], data=json.loads(row["handle_json"]))
        try:
            result = adapter.poll(handle)
        except TransientProviderError as e:
            self._update(
                row["id"],
                next_attempt_at=_iso(now + timedelta(seconds=self.policy.poll_interval_s)),
                last_error=str(e),
            )
            return
        if result.status == "pending":
            self._update(
                row["id"],
                next_attempt_at=_iso(now + timedelta(seconds=self.policy.poll_interval_s)),
            )
            return
        if result.status == "failed":
            tried: list[str] = json.loads(row["tried_providers"])
            self._give_up_on_provider(
                row, provider_id, tried, result.error or "provider job failed", now
            )
            return
        files = adapter.download(handle, self.raw_dir / row["batch_id"])
        if not files:
            tried = json.loads(row["tried_providers"])
            self._give_up_on_provider(row, provider_id, tried, "download returned no files", now)
            return
        self._update(row["id"], status="done", last_error=None)
        report.completed.append(
            CompletedJob(
                job_id=row["id"],
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
            "SELECT * FROM jobs WHERE batch_id = ? AND status IN ('queued', 'submitted')",
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
