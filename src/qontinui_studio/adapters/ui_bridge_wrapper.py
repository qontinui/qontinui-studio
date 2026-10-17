"""A UI Bridge wrapper driving the operator's own web account (Gemini,
ChatGPT), reached through the runner's wrapper dispatch door (plan
2026-10-17-free-image-generation-local-gpu-and-private-ui-bridge-wrappers).

The wrapper runs on the operator's machine, spawned by its runner, against a
browser session the operator logged into by hand. Studio never sees a
credential: it posts ``POST {runner}/wrappers/{wrapper_id}/dispatch`` with
``{"action": "generate-image", "params": {"prompt", "aspectRatio"}}`` and
reads the runner's ``{"success", "data": {"result": ...}, "error"}`` envelope.

The wrapper's ``result`` (the contract this adapter is built against):

* ``{"status": "done", "images": [{"b64_json": ...}, ...]}`` — staged at
  submit time, so ``poll`` reports ``done`` at once;
* ``{"status": "handoff", "reason": ...}`` — a login wall, CAPTCHA, bot
  challenge, usage limit or unrecognised page. The wrapper stopped and did
  nothing further -> ``OperatorHandoff``, which the scheduler treats as a
  spent quota: no retry against the site until the provider's reset;
* ``{"status": "paced", "retry_after_s": n}`` — the wrapper's human-pace
  interval has not elapsed; nothing was typed -> ``ProviderBusy``;
* ``{"status": "cap_reached"}`` — the wrapper's own daily counter is spent
  -> ``QuotaExhaustedError``.

Transport outcomes, typed and never collapsed: the runner unreachable, or
answering 503 (the wrapper could not be spawned) -> ``ProviderBusy`` (nothing
started); 404 (wrapper not installed) and any other 4xx -> permanent; a drop
after sending, a 502/504 or an unreadable body -> ``ProviderOutcomeUnknown``
(the prompt may have been typed).
"""

from __future__ import annotations

import base64
import binascii
import json
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
)
from qontinui_studio.providers import is_local_network

KIND = "ui_bridge_wrapper"
ACTION = "generate-image"
DEFAULT_RUNNER_URL = "http://127.0.0.1:9876"
RUNNER_URL_ENV = "STUDIO_RUNNER_URL"
# One web generation: typing, the site's own queue, rendering and the fetch.
DISPATCH_TIMEOUT_S = 300.0
CONNECT_TIMEOUT_S = 10.0
TIMEOUT_HEADER = "X-Wrapper-Dispatch-Timeout"
# No runner listening, or a wrapper it cannot start, is not a busy site: park
# the job past the scheduler's run horizon (30 min) so ``brief run`` returns at
# once and reports it, rather than re-dialling a dead door for the whole run
# (the same rule as the local facade's refused connection).
UNREACHABLE_RETRY_AFTER_S = 3600.0


class UIBridgeWrapperAdapter:
    def __init__(
        self,
        provider_id: str,
        model: str,
        wrapper_id: str,
        runner_url: str = DEFAULT_RUNNER_URL,
        *,
        client: httpx.Client | None = None,
        dispatch_timeout_s: float = DISPATCH_TIMEOUT_S,
    ) -> None:
        if not is_local_network(runner_url):
            raise PermanentProviderError(
                f"{provider_id}: the runner URL {runner_url!r} is not on this machine or the "
                "local network; the wrapper drives the operator's own browser session"
            )
        self.provider_id = provider_id
        self.model = model
        self.kind = KIND
        self.wrapper_id = wrapper_id
        self.runner_url = runner_url.rstrip("/")
        self.dispatch_timeout_s = dispatch_timeout_s
        self.hard_timeout_s = dispatch_timeout_s + CONNECT_TIMEOUT_S
        self._client = client

    def sent_prompt(self, request: GenerationRequest) -> str:
        return fold_negative(request)

    def sent_parameters(self, request: GenerationRequest) -> dict[str, Any]:
        # The web UI takes a prompt and nothing else; the ratio rides in it.
        return {"aspect_ratio": request.aspect_ratio}

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        body = {
            "action": ACTION,
            "params": {"prompt": self.sent_prompt(request), "aspectRatio": request.aspect_ratio},
        }
        url = f"{self.runner_url}/wrappers/{self.wrapper_id}/dispatch"
        timeout = httpx.Timeout(self.dispatch_timeout_s + 5.0, connect=CONNECT_TIMEOUT_S)
        headers = {TIMEOUT_HEADER: str(int(self.dispatch_timeout_s))}
        client = self._client or httpx.Client()
        try:
            resp = client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.TransportError as e:
            if never_sent(e):
                raise ProviderBusy(
                    f"{self.provider_id}: runner unreachable at {self.runner_url}: {e}",
                    UNREACHABLE_RETRY_AFTER_S,
                    code="runner_unreachable",
                ) from e
            raise ProviderOutcomeUnknown(f"{self.provider_id}: dispatch dropped: {e}") from e
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
        error = envelope.get("error") if isinstance(envelope, dict) else None
        status = resp.status_code
        if status == 503:
            raise ProviderBusy(
                f"{self.provider_id}: runner could not start wrapper '{self.wrapper_id}': {error}",
                UNREACHABLE_RETRY_AFTER_S,
                code="wrapper_unavailable",
            )
        if status in (502, 504) or status >= 500:
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
        if not isinstance(result, dict):
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: runner answered 2xx without a result object"
            )
        return result

    def _stage(
        self, request: GenerationRequest, result: dict[str, Any], staging_dir: Path
    ) -> SubmittedJob:
        state = result.get("status")
        if state == "handoff":
            reason = str(result.get("reason") or "ui_changed")
            if reason not in OperatorHandoff.REASONS:
                reason = "ui_changed"
            raise OperatorHandoff(
                f"operator hand-off needed ({reason}): {self.provider_id} stopped and did "
                f"nothing further; {result.get('detail') or 'resolve it in the browser'}",
                reason,
            )
        if state == "paced":
            raise ProviderBusy(
                f"{self.provider_id}: human-pace interval not yet elapsed",
                clamp_retry_after(result.get("retry_after_s")),
                code="paced",
            )
        if state == "cap_reached":
            raise QuotaExhaustedError(f"{self.provider_id}: the wrapper's daily cap is spent")
        if state != "done":
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: wrapper answered an unknown status {state!r}"
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
        return SubmittedJob(
            external_id=f"{self.wrapper_id}:{request.job_id}",
            data={"staged": staged, "conversation_url": result.get("conversation_url")},
        )

    def poll(self, job: SubmittedJob) -> PollResult:
        return PollResult(status="done")

    def download(self, job: SubmittedJob, dest_dir: Path) -> list[Path]:
        return copy_staged(job, dest_dir)
