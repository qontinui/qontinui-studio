"""A UI Bridge wrapper driving the operator's own web account (Gemini,
ChatGPT), reached through the runner's wrapper dispatch door (plan
2026-10-17-free-image-generation-local-gpu-and-private-ui-bridge-wrappers).

The wrapper runs on the operator's machine, spawned by its runner, against a
browser session the operator logged into by hand. Studio never sees a
credential: it posts ``POST {runner}/wrappers/{wrapper_id}/dispatch`` with
``{"action": "generate-image", "params": {"prompt", "aspectRatio",
"maxPerDay", "minIntervalS"}}`` (the row's override bounds; the wrapper applies
the stricter of its own and these) and reads a TWO-LAYER envelope:
``{"success": true, "data": {"result": {"success": true, "data": <answer>}}}``.
The outer layer is the runner's (dispatch.rs forwards the wrapper's body as
``result``); the inner one is the @qontinui/ui-bridge-wrapper dispatch
server's. A wrapper whose body carries its own ``result`` key reaches us
one layer shallower, and that shape is accepted too.

The wrapper's ``result`` (the contract this adapter is built against):

* ``{"status": "done", "images": [{"b64_json": ...}, ...]}`` — staged at
  submit time, so ``poll`` reports ``done`` at once;
* ``{"status": "handoff", "reason": ...}`` — a login wall, CAPTCHA, bot
  challenge, usage limit, unreadable state or unrecognised page. The wrapper
  stopped, did nothing further and latched it on its side. Studio latches it
  too (``HandoffLatch``), posts one durable notice naming what to do, and
  raises ``OperatorHandoff``. The provider stays out of every run — not just
  until the daily reset — until the operator runs
  ``studio providers clear-handoff <id>``;
* ``{"status": "paced", "retry_after_s": n}`` — the wrapper's human-pace
  interval has not elapsed, or another run holds its lock; nothing was typed
  -> ``ProviderBusy``;
* ``{"status": "cap_reached"}`` — the wrapper's own daily counter is spent
  -> ``QuotaExhaustedError``.

Transport outcomes, typed and never collapsed. Only the RUNNER's typed
refusals mean "nothing started": the runner unreachable, or a 503 carrying
one of its typed codes — ``wrapper_spawn_failed`` (or, from a runner
predating the code, its own ``failed to spawn wrapper`` message),
``wrappers_not_initialized`` (booting) or ``primary_unreachable`` (a
secondary whose request never reached the primary) -> ``ProviderBusy`` parked
past the run horizon; a 429 carrying ``wrapper_busy`` -> ``ProviderBusy`` retried soon. A
404 (wrapper not installed) and any other 4xx except 429 -> permanent. Any
other 429 or 5xx (the runner reports a wrapper's own 5xx as 502, never 503), a
drop after sending, or an unreadable body -> ``ProviderOutcomeUnknown``
(the prompt may have been typed; counted).

Nothing the runner or the wrapper SAYS is persisted. Their free text can carry
a conversation id, a sign-in continuation, an account name or credentials in a
URL, and pattern-based redaction of that text leaks (review of #21, round 4).
So every message, latch file and notice is built only from an allowlisted
structure: an outcome code from a closed set, the provider row id, the wrapper
id, an HTTP status, and the provider's KNOWN host from providers.yaml (never
parsed from text). Any free text is replaced by ``omitted(text)`` — its length
and a sha256 prefix, for correlation only.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from qontinui_studio.adapters.base import (
    GenerationRequest,
    PollResult,
    SubmittedJob,
    copy_staged,
    fold_negative,
    never_sent,
    write_staged,
)
from qontinui_studio.errors import (
    OperatorHandoff,
    PermanentProviderError,
    ProviderBusy,
    ProviderOutcomeUnknown,
    QuotaExhaustedError,
    clamp_retry_after,
    omitted,
)
from qontinui_studio.notify import Notice, Notifier
from qontinui_studio.providers import OperatorOverride, is_local_network

KIND = "ui_bridge_wrapper"
ACTION = "generate-image"
DEFAULT_RUNNER_URL = "http://127.0.0.1:9876"
RUNNER_URL_ENV = "STUDIO_RUNNER_URL"
# One web generation: typing, the site's own queue, rendering and the fetch.
DISPATCH_TIMEOUT_S = 300.0
CONNECT_TIMEOUT_S = 10.0
TIMEOUT_HEADER = "X-Wrapper-Dispatch-Timeout"
# Kept back from the dispatch timeout when telling the wrapper its budget.
BUDGET_MARGIN_S = 20
# Everything the runner may spend BEFORE its dispatch clock starts. ONE number
# on both sides: this mirrors qontinui-runner's wrappers/manager.rs
# PRE_DISPATCH_CEILING, which a runner test pins at 100 s
# (`the_pre_dispatch_ceiling_is_the_documented_100s`) — change both together.
# It is the exclusive turn and the spawn lock under one 30 s wait (dispatch.rs
# PRE_DISPATCH_WAIT, then a 429 `wrapper_busy`), retiring a degraded record
# (15 s), a browser wrapper's readiness (30 s), terminating a spawn that never
# became ready (15 s) and a 10 s margin.
RUNNER_PRE_DISPATCH_CEILING_S = 100.0
PRE_DISPATCH_ALLOWANCE_S = RUNNER_PRE_DISPATCH_CEILING_S
# The runner's typed busy refusal (HTTP 429): the action's turn or the spawn
# lock was in use and NOTHING was started. The wrapper is not broken, so the
# job comes back as soon as a paced run would (the wrapper's `paced` answer).
RUNNER_BUSY_CODE = "wrapper_busy"
RUNNER_BUSY_RETRY_AFTER_S = 30.0
# The codes of the runner's typed "nothing reached the wrapper" 503
# (qontinui-runner dispatch.rs NOTHING_STARTED_CODES): it could not start the
# wrapper, it is still booting, or (through a secondary runner) the request
# never reached the primary. The runner answers a wrapper-originated 5xx as
# 502, but a 503 can still arrive without a code (an older runner, or anything
# else on the port), so one of these codes — not the status — is what says
# "nothing started".
RUNNER_SPAWN_FAILED_CODE = "wrapper_spawn_failed"
RUNNER_NOTHING_STARTED_CODES = frozenset(
    {RUNNER_SPAWN_FAILED_CODE, "wrappers_not_initialized", "primary_unreachable"}
)
# A runner that predates the code says the same thing by message prefix.
LEGACY_SPAWN_FAILED_PREFIX = "failed to spawn wrapper '"
# No runner listening, or a wrapper it cannot start, is not a busy site: park
# the job past the scheduler's run horizon (30 min) so ``brief run`` returns at
# once and reports it, rather than re-dialling a dead door for the whole run
# (the same rule as the local facade's refused connection).
UNREACHABLE_RETRY_AFTER_S = 3600.0


class HandoffLatch:
    """Studio's side of the hand-off latch: one JSON file per provider under the
    workspace. While a provider's file exists it is built as unavailable and
    its adapter answers ``OperatorHandoff`` without dialling. Only the operator
    clears it (``studio providers clear-handoff <id>``)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, provider_id: str) -> Path:
        return self.directory / f"{provider_id}.json"

    def get(self, provider_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(provider_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # Unreadable is still latched: fail closed.
            return {"reason": "state_unreadable"}
        if not isinstance(data, dict) or data.get("reason") not in OperatorHandoff.REASONS:
            return {"reason": "state_unreadable"}
        return data

    def set(self, provider_id: str, reason: str, detail: str | None = None) -> bool:
        """Latch; returns False when it was already latched (notify once). Only
        the reason code, the time and an ``omitted`` digest of any detail are
        written — never the detail itself."""
        path = self._path(provider_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"reason": reason, "at": datetime.now(UTC).isoformat()}
        if detail:
            payload["detail_omitted"] = omitted(detail)
        try:
            with path.open("x", encoding="utf-8") as f:
                json.dump(payload, f)
        except FileExistsError:
            return False
        return True

    def clear(self, provider_id: str) -> bool:
        try:
            self._path(provider_id).unlink()
        except FileNotFoundError:
            return False
        return True

    def describe(self, provider_id: str) -> str | None:
        latch = self.get(provider_id)
        if latch is None:
            return None
        at = latch.get("at")
        when = at if isinstance(at, str) and len(at) <= 40 else "?"
        return (
            f"operator hand-off latched ({latch.get('reason')}) since {when}; resolve it in the "
            "browser, then clear it with "
            f"`studio providers clear-handoff {provider_id}` and, on the wrapper's machine, "
            f"`node <wrappers root>/{provider_id}/dist/cli.js clear-handoff`"
        )


class UIBridgeWrapperAdapter:
    def __init__(
        self,
        provider_id: str,
        model: str,
        wrapper_id: str,
        runner_url: str = DEFAULT_RUNNER_URL,
        *,
        override: OperatorOverride,
        state_dir: Path,
        notifier: Notifier | None = None,
        known_host: str | None = None,
        client: httpx.Client | None = None,
        dispatch_timeout_s: float = DISPATCH_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not is_local_network(runner_url):
            raise PermanentProviderError(
                f"{provider_id}: the configured runner URL is not on this machine or the local "
                "network; the wrapper drives the operator's own browser session"
            )
        self.provider_id = provider_id
        self.model = model
        self.kind = KIND
        self.wrapper_id = wrapper_id
        self.runner_url = runner_url.rstrip("/")
        # Required, not optional: without the bounds no dispatch is bounded,
        # and without a state dir a hand-off could neither latch nor space.
        self.override = override
        self.latch = HandoffLatch(state_dir / "handoffs")
        self.pace_path = state_dir / "wrapper-pace" / f"{provider_id}.json"
        self.notifier = notifier
        # The provider's own host as providers.yaml declares it — the only
        # location any message may name. Never parsed from the wrapper's text.
        self.known_host = known_host
        self.clock = clock
        self.dispatch_timeout_s = dispatch_timeout_s
        # The runner's dispatch timeout covers only the wrapper's answer; the
        # exclusive-turn wait and a cold spawn come BEFORE that clock starts,
        # so the read timeout allows for both on top.
        self.read_timeout_s = dispatch_timeout_s + PRE_DISPATCH_ALLOWANCE_S + 5.0
        self.hard_timeout_s = self.read_timeout_s + CONNECT_TIMEOUT_S
        self._client = client

    def sent_prompt(self, request: GenerationRequest) -> str:
        return fold_negative(request)

    def sent_parameters(self, request: GenerationRequest) -> dict[str, Any]:
        # The web UI takes a prompt and nothing else; the ratio rides in it.
        return {"aspect_ratio": request.aspect_ratio}

    def _params(self, request: GenerationRequest) -> dict[str, Any]:
        return {
            "prompt": self.sent_prompt(request),
            "aspectRatio": request.aspect_ratio,
            "maxPerDay": self.override.max_per_day,
            "minIntervalS": self.override.min_interval_s,
            # The wrapper's whole-run budget: inside the dispatch timeout this
            # adapter asks the runner for, so a slow success is answered
            # before the runner gives up on it (a success lost on the wire is
            # still counted against the day's cap).
            "budgetS": max(1, int(self.dispatch_timeout_s) - BUDGET_MARGIN_S),
        }

    def _space(self) -> None:
        """Studio's own copy of the human-pace interval: the last dispatch to
        this provider is recorded BEFORE it is sent, and a dispatch inside the
        interval is refused as ``paced`` without dialling, so studio alone holds
        the interval even against a wrapper that does not."""
        now = self.clock()
        try:
            last = float(json.loads(self.pace_path.read_text(encoding="utf-8"))["last_dispatch_at"])
        except FileNotFoundError:
            last = None
        except (OSError, ValueError, KeyError, TypeError):
            last = now  # unreadable: fail closed, wait one full interval
        if last is not None:
            wait = self.override.min_interval_s - (now - last)
            if wait > 0:
                raise ProviderBusy(
                    f"{self.provider_id}: studio-side pace interval not yet elapsed",
                    clamp_retry_after(wait),
                    code="paced",
                )
        self.pace_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pace_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_dispatch_at": now}), encoding="utf-8")
        tmp.replace(self.pace_path)

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        latched = self.latch.describe(self.provider_id)
        if latched is not None:
            reason = str((self.latch.get(self.provider_id) or {}).get("reason", "ui_changed"))
            raise OperatorHandoff(latched, reason)
        self._space()
        body = {"action": ACTION, "params": self._params(request)}
        url = f"{self.runner_url}/wrappers/{self.wrapper_id}/dispatch"
        timeout = httpx.Timeout(self.read_timeout_s, connect=CONNECT_TIMEOUT_S)
        headers = {TIMEOUT_HEADER: str(int(self.dispatch_timeout_s))}
        client = self._client or httpx.Client()
        try:
            resp = client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.TransportError as e:
            if never_sent(e):
                raise ProviderBusy(
                    f"{self.provider_id}: runner unreachable (transport_error {type(e).__name__})",
                    UNREACHABLE_RETRY_AFTER_S,
                    code="runner_unreachable",
                ) from e
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: dispatch dropped after sending (transport_error "
                f"{type(e).__name__})"
            ) from e
        except httpx.HTTPError as e:
            # e.g. httpx.DecodingError (a body that fails its content-encoding):
            # not a TransportError, but still a request that may have run.
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: dispatch answer unreadable ({type(e).__name__})"
            ) from e
        finally:
            if self._client is None:
                client.close()
        result = self._result(resp)
        return self._stage(request, result, staging_dir)

    def _result(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            envelope = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: runner answered HTTP {resp.status_code} with a non-JSON body"
            ) from e
        raw_error = envelope.get("error") if isinstance(envelope, dict) else None
        error = omitted(raw_error) if isinstance(envelope, dict) else "no body"
        status = resp.status_code
        if status == 429 and self._runner_busy(envelope, raw_error):
            # The runner's TYPED busy refusal only: nothing was started, so it
            # is refunded and retried soon.
            raise ProviderBusy(
                f"{self.provider_id}: wrapper '{self.wrapper_id}' is busy "
                f"({RUNNER_BUSY_CODE}); nothing was started",
                RUNNER_BUSY_RETRY_AFTER_S,
                code=RUNNER_BUSY_CODE,
            )
        if status == 503 and self._runner_nothing_started(envelope, raw_error):
            # The runner's TYPED "nothing started" 503 only: nothing reached the
            # wrapper, so it is refunded and parked. An untyped 503 (a wrapper's
            # own, through a proxy that passes statuses on) is not known to mean
            # "nothing started" and falls through to the 5xx handling below.
            raise ProviderBusy(
                f"{self.provider_id}: runner could not start wrapper '{self.wrapper_id}': {error}",
                UNREACHABLE_RETRY_AFTER_S,
                code="wrapper_unavailable",
            )
        if status == 429:
            # A 429 that is not the runner's typed busy came from the WRAPPER,
            # which may have sent the prompt before refusing: counted, like any
            # outcome that may have run.
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: wrapper '{self.wrapper_id}' answered HTTP 429 after the "
                f"dispatch reached it: {error}"
            )
        if status >= 500:
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: wrapper dispatch failed after sending (HTTP {status}): "
                f"{error}"
            )
        if status >= 400:
            raise PermanentProviderError(
                f"{self.provider_id}: wrapper '{self.wrapper_id}' refused (HTTP {status}): {error}"
            )
        data = envelope.get("data") if isinstance(envelope, dict) else None
        result = data.get("result") if isinstance(data, dict) else None
        # The runner forwards the wrapper's body as ``result`` (dispatch.rs: the
        # body's own ``result`` key if it has one, else the whole body). The
        # @qontinui/ui-bridge-wrapper dispatch server answers
        # ``{"success": true, "data": <handler result>}``, so the handler's
        # answer sits one level further in. Both shapes are accepted, explicitly.
        if isinstance(result, dict) and "status" not in result and "success" in result:
            if result.get("success") is not True:
                raise ProviderOutcomeUnknown(
                    f"{self.provider_id}: wrapper reported failure; {omitted(result.get('error'))}"
                )
            result = result.get("data")
        if not isinstance(result, dict):
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: runner answered 2xx without a result object"
            )
        return result

    @staticmethod
    def _runner_nothing_started(envelope: object, raw_error: object) -> bool:
        """One of the runner's typed "nothing started" codes in its envelope's
        `code`, or — for a runner that predates the field — its own spawn
        failure's message prefix (a wrapper's error is always reported as
        ``wrapper '<id>' returned …``, so it cannot carry this prefix). Never
        echoed."""
        code = envelope.get("code") if isinstance(envelope, dict) else None
        if isinstance(code, str) and code in RUNNER_NOTHING_STARTED_CODES:
            return True
        return isinstance(raw_error, str) and raw_error.startswith(LEGACY_SPAWN_FAILED_PREFIX)

    @staticmethod
    def _runner_busy(envelope: object, raw_error: object) -> bool:
        """The runner's typed `wrapper_busy`: its envelope's `code` field, or —
        for a runner that predates the field — the message prefix. Only the
        code is matched; the text itself is never echoed."""
        if isinstance(envelope, dict) and envelope.get("code") == RUNNER_BUSY_CODE:
            return True
        return isinstance(raw_error, str) and raw_error.startswith(f"{RUNNER_BUSY_CODE}:")

    def _hand_off(self, reason: str, detail: str | None) -> OperatorHandoff:
        where = f" at {self.known_host}" if self.known_host else ""
        message = (
            f"operator hand-off needed ({reason}): {self.provider_id}{where} stopped and did "
            "nothing further"
        )
        if self.latch.set(self.provider_id, reason, detail):
            if self.notifier is not None:
                self.notifier.notify(
                    Notice(
                        batch_id=self.provider_id,
                        outcome="handoff",
                        title=f"{self.provider_id} needs the operator ({reason})",
                        body=self.latch.describe(self.provider_id) or message,
                    )
                )
        return OperatorHandoff(message, reason)

    def _stage(
        self, request: GenerationRequest, result: dict[str, Any], staging_dir: Path
    ) -> SubmittedJob:
        state = result.get("status")
        if state == "handoff":
            reason = str(result.get("reason") or "ui_changed")
            if reason not in OperatorHandoff.REASONS:
                reason = "ui_changed"
            raise self._hand_off(reason, str(result.get("detail") or ""))
        if state == "paced":
            raise ProviderBusy(
                f"{self.provider_id}: human-pace interval not yet elapsed",
                clamp_retry_after(result.get("retry_after_s")),
                code="paced",
            )
        if state == "timeout":
            # The wrapper's budget ran out (a slow site or machine): never the
            # operator's to resolve, so never a hand-off. Whether the PROMPT
            # LEFT decides the rest. Not typed: provably nothing started, a
            # busy refusal (refunded, no attempt spent). Typed: the site has the
            # prompt and the wrapper counted it, so it must cost an attempt —
            # a refund here would let an always-slow site be retried every
            # minute until the wrapper's own cap stopped it.
            if result.get("typed") is True:
                raise ProviderOutcomeUnknown(
                    f"{self.provider_id}: the wrapper's run budget ran out after the prompt "
                    "was sent"
                )
            raise ProviderBusy(
                f"{self.provider_id}: the wrapper's run budget ran out before the prompt was sent",
                clamp_retry_after(result.get("retry_after_s")),
                code="timeout",
            )
        if state == "cap_reached":
            raise QuotaExhaustedError(f"{self.provider_id}: the wrapper's daily cap is spent")
        if state != "done":
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: wrapper answered an unknown status; {omitted(state)}"
            )
        images = result.get("images")
        if not isinstance(images, list) or not images:
            raise PermanentProviderError(f"{self.provider_id}: wrapper returned no image")
        staged: list[str] = []
        for i, item in enumerate(images):
            raw = item.get("b64_json") if isinstance(item, dict) else None
            if not isinstance(raw, str):
                raise PermanentProviderError(f"{self.provider_id}: image {i} carries no b64_json")
            try:
                data = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as e:
                raise PermanentProviderError(
                    f"{self.provider_id}: image {i} is not valid base64"
                ) from e
            staged.append(str(write_staged(staging_dir, f"{request.job_id}-{i}", data)))
        # Only the staged paths are kept: no conversation URL (it can identify
        # the account) ever reaches the job's handle.
        return SubmittedJob(
            external_id=f"{self.wrapper_id}:{request.job_id}", data={"staged": staged}
        )

    def poll(self, job: SubmittedJob) -> PollResult:
        return PollResult(status="done")

    def download(self, job: SubmittedJob, dest_dir: Path) -> list[Path]:
        return copy_staged(job, dest_dir)
