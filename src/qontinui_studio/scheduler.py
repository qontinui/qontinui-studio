"""Job scheduler: spreads generation across each provider's daily free quota.

State is persisted in SQLite, so a crashed or quota-deferred run resumes where
it stopped. Per job the scheduler walks the brief's provider preference,
skipping any provider that is unschedulable (D4 / no free quota), whose adapter
could not be built, or whose quota for the current UTC day is spent. A
transient failure is retried with exponential backoff; once a provider has
exhausted its attempts the job moves to the next preferred provider. When every
preferred provider is spent for today, the job is deferred to the next UTC
midnight rather than failed.

Typed job states (plan 2026-10-13 Phase 11): every provider-side outcome is
written to ``job_attempts`` as ``done | failed | unknown | deferred | busy``.

* A host's ``deferred``/``busy`` refusal requeues the job at the host's
  ``retry_after_s`` WITHOUT spending a quota unit (the unit spent at the claim
  is refunded) or an attempt. A ``deferred``, or a ``busy`` answered to a bulk
  job, never moves the job to another provider, so bulk work cannot spill onto
  a paid adapter.
* ``unknown`` (the transport failed mid-flight, or the job id is lost) is
  recorded as itself and the job is retried as a fresh, counted attempt.
* An interactive job whose provider answered ``busy`` or ``unknown`` may fall
  back to the next preferred provider — but only one with capacity now, so a
  cloud adapter is used only within its own recorded quota (none is recorded
  today, which means no cloud work).
* An ``unmetered`` provider (a self-hosted host on the local network) spends
  no quota; the host's own refusals bound it.

Concurrency: every step that acts on a job first CLAIMS it with a fresh token
(``jobs.claim``) in a conditional update, and every later write is conditional
on that token and the expected status. A process whose claim was taken over —
because it outlived its lease — has its late result discarded, never merged
with another process's state. A submission's lease is longer than the adapter's
hard timeout, so a live request is never reclaimed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from qontinui_studio.adapters import AdapterSet, GenerationRequest, SubmittedJob
from qontinui_studio.errors import (
    PermanentProviderError,
    ProviderBusy,
    ProviderDeferred,
    ProviderOutcomeUnknown,
    ProviderRefusal,
    QuotaExhaustedError,
    TransientProviderError,
)
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


# A provider quota message from an adapter that raised a plain transient error.
_QUOTA_TEXT = re.compile(r"insufficient_quota|per[ _-]?day|daily", re.IGNORECASE)


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

    def quota_day(self, provider_id: str, now: datetime) -> str:
        """The provider's quota day: the date in its reset timezone."""
        tz = ZoneInfo(self.inventory.get(provider_id).quota_reset_tz)
        return now.astimezone(tz).date().isoformat()

    def next_reset(self, provider_id: str, now: datetime) -> datetime:
        """The next midnight in the provider's reset timezone, in UTC."""
        tz = ZoneInfo(self.inventory.get(provider_id).quota_reset_tz)
        local = now.astimezone(tz)
        midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(UTC)

    def quota_used(self, provider_id: str, day: str) -> int:
        row = self.conn.execute(
            "SELECT used FROM quota_usage WHERE provider = ? AND day = ?", (provider_id, day)
        ).fetchone()
        return int(row["used"]) if row else 0

    def spent_until(self, provider_id: str, now: datetime) -> datetime | None:
        row = self.conn.execute(
            "SELECT until FROM provider_spent WHERE provider = ?", (provider_id,)
        ).fetchone()
        if row is None:
            return None
        until = _parse(row["until"])
        return until if until > now else None

    def quota_remaining(self, provider_id: str, now: datetime) -> int:
        provider = self.inventory.get(provider_id)
        if provider.schedulable_reason() is not None or provider.daily_quota is None:
            return 0
        if self.spent_until(provider_id, now) is not None:
            return 0
        used = self.quota_used(provider_id, self.quota_day(provider_id, now))
        return max(0, provider.daily_quota - used)

    def has_capacity(self, provider_id: str, now: datetime) -> bool:
        """Whether the scheduler may assign this provider a job now: an
        unmetered provider whenever it is schedulable, a metered one while its
        quota for the day lasts."""
        provider = self.inventory.get(provider_id)
        if provider.capacity == "unmetered":
            return provider.schedulable_reason() is None
        return self.quota_remaining(provider_id, now) > 0

    def _refund(self, provider_id: str, day: str | None) -> None:
        """Give back the unit a refused submission spent (none when unmetered)."""
        if day is None:
            return
        self.conn.execute(
            "UPDATE quota_usage SET used = used - 1 WHERE provider = ? AND day = ? AND used > 0",
            (provider_id, day),
        )
        self.conn.commit()

    def _attempt(
        self,
        job_id: str,
        provider_id: str,
        outcome: str,
        detail: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO job_attempts (job_id, provider, outcome, detail, retry_after_s, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, provider_id, outcome, detail, retry_after_s, _iso(self.clock())),
        )
        self.conn.commit()

    def attempts(self, job_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM job_attempts WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()

    def _mark_spent(self, provider_id: str, now: datetime) -> None:
        until = self.next_reset(provider_id, now)
        self.conn.execute(
            "INSERT INTO provider_spent (provider, until) VALUES (?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET until = excluded.until",
            (provider_id, _iso(until)),
        )
        self.conn.commit()

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
            if self.has_capacity(pid, now):
                return pid
        return None

    def _next_window(
        self, preference: list[str], tried: list[str], now: datetime
    ) -> datetime | None:
        """When some untried preferred provider (adapter built, schedulable) gets
        quota again — the earliest of their resets — or None if none ever will."""
        times: list[datetime] = []
        for pid in preference:
            if pid in tried or pid not in self.adapters.adapters:
                continue
            provider = self.inventory.get(pid)
            if provider.schedulable_reason() is not None or provider.capacity == "unmetered":
                continue
            times.append(self.spent_until(pid, now) or self.next_reset(pid, now))
        return min(times) if times else None

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
                if row["provider"] is not None:
                    self._attempt(
                        row["id"],
                        row["provider"],
                        "unknown",
                        "a submission's claim outlived its lease; its process is gone and "
                        "whether the provider ran the job is unknown",
                    )
                row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            if row["status"] == "queued":
                self._submit(row, preference, template, now, report)
            elif row["status"] == "submitted":
                self._poll(row, preference, template, report)
        self.conn.commit()
        return report

    def _spend_and_claim(
        self, row: sqlite3.Row, provider_id: str, token: str, fields: dict[str, object]
    ) -> str:
        """Atomically spend one unit of the provider's quota AND claim the job.
        Returns 'ok', 'no_quota' (the last unit went elsewhere) or 'lost'
        (another process claimed the job). Nothing changes unless 'ok'. An
        unmetered provider spends nothing; ``quota_day`` records the day a unit
        was spent from (NULL when none was)."""
        provider = self.inventory.get(provider_id)
        now = self.clock()
        metered = provider.capacity != "unmetered"
        day = self.quota_day(provider_id, now) if metered else None
        fields = {**fields, "quota_day": day}
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if metered:
                self.conn.execute(
                    "INSERT OR IGNORE INTO quota_usage (provider, day, used) VALUES (?, ?, 0)",
                    (provider_id, day),
                )
                spent = self.conn.execute(
                    "UPDATE quota_usage SET used = used + 1 "
                    "WHERE provider = ? AND day = ? AND used < ?",
                    (provider_id, day, provider.daily_quota or 0),
                ).rowcount
                if spent != 1:
                    self.conn.rollback()
                    return "no_quota"
            cols = ", ".join(f"{k} = ?" for k in fields)
            claimed = self.conn.execute(
                f"UPDATE jobs SET status = 'submitting', claim = ?, {cols} "
                "WHERE id = ? AND status = 'queued' AND claim IS ?",
                (token, *fields.values(), row["id"], row["claim"]),
            ).rowcount
            if claimed != 1:
                self.conn.rollback()
                return "lost"
            self.conn.commit()
            return "ok"
        except BaseException:
            self.conn.rollback()
            raise

    def _submit(
        self,
        row: sqlite3.Row,
        preference: list[str],
        template: GenerationRequest,
        _step_start: datetime,
        report: StepReport,
    ) -> None:
        tried: list[str] = json.loads(row["tried_providers"])
        excluded: set[str] = set()
        token = uuid.uuid4().hex
        while True:
            now = self.clock()  # read at the claim, never at step start
            provider_id = row["provider"]
            if (
                provider_id is None
                or provider_id in tried
                or provider_id in excluded
                or provider_id not in self.adapters.adapters  # not built this run
                or not self.has_capacity(provider_id, now)
            ):
                provider_id = self._pick_provider(preference, [*tried, *excluded], now)
            if provider_id is None:
                window = self._next_window(preference, tried, now)
                if window is not None:
                    self._write(
                        row["id"],
                        row["claim"],
                        "queued",
                        provider=None,
                        next_attempt_at=_iso(window),
                        last_error="every preferred provider's quota is spent; deferred to "
                        f"its reset at {_iso(window)}",
                    )
                elif self._write(
                    row["id"],
                    row["claim"],
                    "queued",
                    status="failed",
                    last_error=row["last_error"]
                    or "no schedulable provider in the brief's preference",
                ):
                    report.failed.append(row["id"])
                return
            adapter = self.adapters.adapters[provider_id]
            request = self._request(row, template)
            lease = self._lease(adapter.hard_timeout_s, self.policy.submit_stale_s)
            # The claim records exactly what is being sent (prompt, model) and the
            # terms as reviewed right now (D5); ingest reads only these.
            outcome = self._spend_and_claim(
                row,
                provider_id,
                token,
                {
                    "provider": provider_id,
                    "prompt": adapter.sent_prompt(request),
                    "model": adapter.model,
                    "adapter": adapter.kind,
                    "terms_json": TermsSnapshot.of(self.inventory.get(provider_id)).to_json(),
                    "next_attempt_at": _iso(now + timedelta(seconds=lease)),
                    "updated_at": _iso(now),
                },
            )
            if outcome == "lost":
                return
            if outcome == "no_quota":
                excluded.add(provider_id)
                continue
            break

        attempts = row["attempts"] if provider_id == row["provider"] else 0
        spent = self.conn.execute(
            "SELECT quota_day FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        quota_day = spent["quota_day"] if spent is not None else None
        try:
            handle = adapter.submit(request, self.staging_dir / row["id"] / token)
        except ProviderRefusal as e:
            self._after_refusal(
                row["id"],
                token,
                "submitting",
                provider_id,
                e,
                template,
                preference,
                tried,
                quota_day,
                attempts,
            )
            return
        except ProviderOutcomeUnknown as e:
            self._after_unknown(
                row["id"],
                token,
                "submitting",
                provider_id,
                str(e),
                template,
                preference,
                tried,
                attempts + 1,
            )
            return
        except QuotaExhaustedError as e:
            self._quota_exhausted(row["id"], token, "submitting", provider_id, str(e))
            return
        except TransientProviderError as e:
            if _QUOTA_TEXT.search(str(e)):
                # An adapter that did not type its quota error.
                self._quota_exhausted(row["id"], token, "submitting", provider_id, str(e))
                return
            self._attempt(row["id"], provider_id, "failed", f"transient: {e}", e.retry_after_s)
            now = self.clock()
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
            self._attempt(row["id"], provider_id, "failed", str(e))
            self._give_up_on_provider(
                row["id"], token, "submitting", provider_id, tried, str(e), self.clock()
            )
            return
        now = self.clock()
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
        self._poll(fresh, preference, template, report)

    @staticmethod
    def _request(row: sqlite3.Row, template: GenerationRequest) -> GenerationRequest:
        return GenerationRequest(
            job_id=row["id"],
            prompt=template.prompt,
            aspect_ratio=template.aspect_ratio,
            reference_images=template.reference_images,
            negative_prompt=template.negative_prompt,
            seed=template.seed,
            recipe=template.recipe,
            priority=template.priority,
        )

    def _fallback(
        self, preference: list[str], tried: list[str], current: str, template: GenerationRequest
    ) -> str | None:
        """The provider an INTERACTIVE job may fall back to after ``busy`` or
        ``unknown``: the next preferred provider with capacity now. A metered
        one therefore only ever within its recorded quota. Bulk jobs never."""
        if template.priority != "interactive":
            return None
        return self._pick_provider(preference, [*tried, current], self.clock())

    def _requeue(self, job_id: str, claim: str, status: str, **fields: object) -> bool:
        return self._write(
            job_id,
            claim,
            status,
            status="queued",
            claim=None,
            polls=0,
            submitted_at=None,
            **fields,
        )

    def _after_refusal(
        self,
        job_id: str,
        claim: str,
        status: str,
        provider_id: str,
        refusal: ProviderRefusal,
        template: GenerationRequest,
        preference: list[str],
        tried: list[str],
        quota_day: str | None,
        attempts: int,
    ) -> None:
        """A host's typed ``deferred``/``busy``: the job did not run. Refund
        the unit, keep the attempt count (``attempts``, as it stood before this
        submission), and come back when the host says."""
        self._refund(provider_id, quota_day)
        self._attempt(job_id, provider_id, refusal.kind, str(refusal), refusal.retry_after_s)
        now = self.clock()
        if refusal.kind == "busy":
            fallback = self._fallback(preference, tried, provider_id, template)
            if fallback is not None:
                self._requeue(
                    job_id,
                    claim,
                    status,
                    provider=fallback,
                    attempts=0,
                    next_attempt_at=_iso(now),
                    last_error=f"{provider_id}: {refusal}; falling back to {fallback}",
                )
                return
        self._requeue(
            job_id,
            claim,
            status,
            provider=provider_id,
            attempts=attempts,
            next_attempt_at=_iso(now + timedelta(seconds=refusal.retry_after_s)),
            last_error=f"{provider_id}: {refusal} (retry after {refusal.retry_after_s:.0f}s)",
        )

    def _after_unknown(
        self,
        job_id: str,
        claim: str,
        status: str,
        provider_id: str,
        error: str,
        template: GenerationRequest,
        preference: list[str],
        tried: list[str],
        attempts: int,
    ) -> None:
        """The outcome is UNKNOWN: recorded as such (never failed or done), and
        the job is retried as a counted attempt — on another provider when an
        interactive job may fall back."""
        self._attempt(job_id, provider_id, "unknown", error)
        now = self.clock()
        fallback = self._fallback(preference, tried, provider_id, template)
        if fallback is not None:
            self._requeue(
                job_id,
                claim,
                status,
                provider=fallback,
                attempts=0,
                next_attempt_at=_iso(now),
                last_error=f"{provider_id}: unknown: {error}; falling back to {fallback}",
            )
            return
        if attempts >= self.policy.max_attempts_per_provider:
            self._give_up_on_provider(
                job_id, claim, status, provider_id, tried, f"unknown: {error}", now
            )
            return
        self._requeue(
            job_id,
            claim,
            status,
            provider=provider_id,
            attempts=attempts,
            next_attempt_at=_iso(now + timedelta(seconds=self.policy.backoff(attempts, None))),
            last_error=f"{provider_id}: unknown: {error}",
        )

    def _quota_exhausted(
        self, job_id: str, claim: str, status: str, provider_id: str, error: str
    ) -> None:
        """The provider's quota is spent: mark it spent until its reset and put
        the job back — NOT as a tried provider and NOT as a failed attempt."""
        now = self.clock()
        self._mark_spent(provider_id, now)
        self._write(
            job_id,
            claim,
            status,
            status="queued",
            claim=None,
            provider=None,
            next_attempt_at=_iso(now),
            last_error=f"{provider_id}: quota exhausted until {_iso(self.next_reset(provider_id, now))}: {error}",
        )

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

    def _poll(
        self,
        row: sqlite3.Row,
        preference: list[str],
        template: GenerationRequest,
        report: StepReport,
    ) -> None:
        now = self.clock()  # read at the claim, never at step start
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
            self._attempt(job_id, provider_id, "failed", str(e))
            give_up(str(e))
            return
        except ProviderRefusal as e:
            self._after_refusal(
                job_id,
                token,
                "submitted",
                provider_id,
                e,
                template,
                preference,
                tried,
                row["quota_day"],
                # The submission counted an attempt; a refusal gives it back.
                max(0, int(row["attempts"]) - 1),
            )
            return
        except ProviderOutcomeUnknown as e:
            self._after_unknown(
                job_id,
                token,
                "submitted",
                provider_id,
                str(e),
                template,
                preference,
                tried,
                int(row["attempts"]),
            )
            return
        except TransientProviderError as e:
            if overdue:
                give_up(f"poll deadline passed: {e}")
            else:
                release(polls=polls, next_attempt_at=next_poll, last_error=str(e))
            return
        if result.status in ("queued", "running"):
            if overdue:
                give_up(
                    f"still {result.status} after {polls} polls / "
                    f"{(now - submitted_at).total_seconds():.0f}s"
                )
            else:
                release(polls=polls, next_attempt_at=next_poll)
            return
        if result.status == "failed":
            self._attempt(job_id, provider_id, "failed", result.error)
            give_up(result.error or "provider job failed")
            return
        if result.status in ("deferred", "busy"):
            refusal_cls = ProviderDeferred if result.status == "deferred" else ProviderBusy
            self._after_refusal(
                job_id,
                token,
                "submitted",
                provider_id,
                refusal_cls(
                    f"{result.status}: {result.error or 'host refused the job'}",
                    result.retry_after_s if result.retry_after_s is not None else 30.0,
                ),
                template,
                preference,
                tried,
                row["quota_day"],
                # The submission counted an attempt; a refusal gives it back.
                max(0, int(row["attempts"]) - 1),
            )
            return
        if result.status == "unknown":
            self._after_unknown(
                job_id,
                token,
                "submitted",
                provider_id,
                result.error or "the provider no longer knows this job",
                template,
                preference,
                tried,
                int(row["attempts"]),
            )
            return
        if result.status != "done":
            # A status this scheduler does not know is not a success.
            self._after_unknown(
                job_id,
                token,
                "submitted",
                provider_id,
                f"unrecognised job status '{result.status}'",
                template,
                preference,
                tried,
                int(row["attempts"]),
            )
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
        self._attempt(job_id, provider_id, "done")
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
        on_iteration: Callable[[], None] | None = None,
    ) -> StepReport:
        """Step until every job of the given batches is terminal or deferred
        beyond ``horizon`` (e.g. to tomorrow's quota)."""
        total = StepReport()
        while True:
            if on_iteration is not None:
                on_iteration()  # e.g. renew the batch lease; raises to stop
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
