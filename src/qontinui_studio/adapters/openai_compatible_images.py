"""The local provider: an OpenAI-compatible images host (plan 2026-10-13
Phases 10-11) — the image facade behind llama-swap on spaceship's GPU, or any
host speaking the same wire.

A sibling of ``openai_images``, not an extension of it: the inventory admits
one row per API adapter kind, that adapter requires an API key, and a loopback
facade has none (an optional ``api_key_env`` is still honoured).

The wire (the facade contract this adapter is built against):

* ``POST {base}/images/generations`` (JSON) or ``{base}/images/edits``
  (``multipart/form-data`` — llama-swap routes edits by ``model`` only in that
  shape) with ``model`` = the provider row's alias and ``stream: true``. The
  extra fields ride in the body the way the OpenAI SDK's ``extra_body`` puts
  them there: ``seed``, ``recipe``, ``negative_prompt`` (the brief's own —
  never folded into the prompt, which would pull a diffusion model TOWARD the
  avoided concept) and ``priority`` (``interactive`` | ``bulk``).
* A streamed answer is SSE under OpenAI's event names. ``qontinui.queued`` /
  ``qontinui.running`` are progress; ``image_generation.completed``
  (``image_edit.completed`` for edits) carries ``b64_json`` and
  ``qontinui_provenance``; ``error`` carries the refusal envelope. A
  non-streamed answer is the OpenAI JSON shape, each ``data[]`` item carrying
  the same two fields.
* Every refusal is ``{"error": {"type": "busy"|"deferred"|"disabled", "code":
  ..., "retry_after_s": ...}}`` plus a ``Retry-After`` header. The adapter
  branches on ``error.type``, never on the status alone (llama-swap's own 429
  is ``rate_limit_exceeded``, which is also a ``busy``).

How each outcome maps (typed, never collapsed):

* connection refused / connect timeout -> ``ProviderBusy``: the job provably
  never started;
* ``busy`` / ``rate_limit_exceeded`` -> ``ProviderBusy``; ``deferred`` ->
  ``ProviderDeferred``; ``disabled`` -> ``ProviderDisabled``;
* ``weights_unverified`` and other 4xx -> ``PermanentProviderError``;
* a drop after the request was sent, a stream that ends with no terminal
  event, an untyped 5xx (a proxy whose upstream died) or an unreadable 2xx ->
  ``ProviderOutcomeUnknown``;
* a result without complete ``qontinui_provenance`` (SPDX licence, recipe
  hash, weight sha256s, seed) -> ``PermanentProviderError``: an unprovenanced
  image never enters the library (decision record a5ec7670).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from qontinui_studio import provenance
from qontinui_studio.adapters.base import (
    GenerationRequest,
    PollResult,
    SubmittedJob,
    copy_staged,
    never_sent,
    read_reference,
    sniff_mime,
    write_staged,
)
from qontinui_studio.errors import (
    DEFAULT_RETRY_AFTER_S,
    MissingCredentialError,
    PermanentProviderError,
    ProviderBusy,
    ProviderDeferred,
    ProviderDisabled,
    ProviderOutcomeUnknown,
    clamp_retry_after,
)

# A refused connection means no host is listening at all — not a busy card.
# The job is parked past the scheduler's run horizon (30 min), so ``brief run``
# returns at once and reports it instead of re-dialling a dead port for as long
# as the command runs; ``studio jobs run`` resumes it once it is due.
CONNECT_REFUSED_RETRY_AFTER_S = 3600.0
# The longest SILENT gap allowed while a job runs (httpx read timeouts are per
# chunk, so a stream sending progress events never trips it).
READ_TIMEOUT_S = 120.0
# The wall-clock deadline across the WHOLE request, progress events included.
# The facade bounds an interactive job (wait + run) well under 300 s. Checked
# per event, so the worst case is this plus one silent gap plus the connect —
# which ``hard_timeout_s`` covers, keeping the call inside the scheduler's
# claim lease (hard timeout + 60 s): a live request is never reclaimed and
# resubmitted.
STREAM_DEADLINE_S = 480.0
CONNECT_TIMEOUT_S = 10.0
# About one megapixel, sides a multiple of 64 — what the D8 diffusion models take.
TARGET_PIXELS = 1024 * 1024
SIDE_MULTIPLE = 64

TERMINAL_EVENTS = ("image_generation.completed", "image_edit.completed")


def size_for_ratio(aspect_ratio: str) -> str:
    w, h = (int(x) for x in aspect_ratio.split(":"))
    width = math.sqrt(TARGET_PIXELS * w / h)
    height = width * h / w

    def snap(v: float) -> int:
        return max(SIDE_MULTIPLE, round(v / SIDE_MULTIPLE) * SIDE_MULTIPLE)

    return f"{snap(width)}x{snap(height)}"


def _retry_after(resp_headers: httpx.Headers | None, err: dict[str, Any], default: float) -> float:
    """The host's retry delay: the envelope's ``retry_after_s``, else the
    ``Retry-After`` header, else ``default``. A non-finite or negative value is
    invalid and skipped; a valid one is clamped (``clamp_retry_after``)."""
    candidates: list[object] = [err.get("retry_after_s")]
    header = resp_headers.get("retry-after") if resp_headers is not None else None
    if header:
        try:
            candidates.append(float(header))
        except ValueError:
            pass
    for value in candidates:
        if clamp_retry_after(value, default=-1.0) >= 0:
            return clamp_retry_after(value)
    return default


def _refusal(err: dict[str, Any], status: int | None, headers: httpx.Headers | None) -> Exception:
    """The typed exception for one error envelope (an HTTP error body or an
    SSE ``error`` event)."""
    etype = str(err.get("type") or "")
    code = str(err.get("code") or "") or None
    message = str(err.get("message") or etype or "error")
    where = f"HTTP {status}" if status is not None else "stream"
    if etype == "busy" or etype == "rate_limit_exceeded" or code == "rate_limit_exceeded":
        detail = {k: v for k, v in err.items() if k == "vram_held_by"}
        return ProviderBusy(
            f"busy ({where}): {message}",
            _retry_after(headers, err, DEFAULT_RETRY_AFTER_S),
            code=code,
            **detail,
        )
    if etype == "deferred":
        return ProviderDeferred(
            f"deferred ({where}): {message}",
            _retry_after(headers, err, DEFAULT_RETRY_AFTER_S),
            code=code,
        )
    if etype == "disabled":
        return ProviderDisabled(f"disabled ({where}): {message}")
    if etype == "weights_unverified" or code == "weights_unverified":
        return PermanentProviderError(f"weights_unverified ({where}): {message}")
    if status is None or 400 <= status < 500:
        return PermanentProviderError(f"failed ({where}, type '{etype}'): {message}")
    # An untyped 5xx: the proxy answered, but whether the job ran is not said.
    return ProviderOutcomeUnknown(f"untyped {where} ({etype or 'no type'}): {message}")


def _error_envelope(raw: bytes) -> dict[str, Any] | None:
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else None


def _sse_events(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    """(event name, data) per SSE event; the name falls back to ``data.type``."""
    event = ""
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field_name == "event":
            event = value
        elif field_name == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


class OpenAICompatibleImageAdapter:
    def __init__(
        self,
        provider_id: str,
        model: str,
        base_url: str,
        api_key_env: str | None = None,
        http: httpx.Client | None = None,
        deadline_s: float = STREAM_DEADLINE_S,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self.kind = provenance.LOCAL_ADAPTER
        self._deadline_s = deadline_s
        self.hard_timeout_s = deadline_s + READ_TIMEOUT_S + CONNECT_TIMEOUT_S
        self._base = base_url.rstrip("/")
        self._headers: dict[str, str] = {}
        if api_key_env:
            key = os.environ.get(api_key_env)
            if not key:
                raise MissingCredentialError(f"{provider_id}: env var {api_key_env} is unset")
            self._headers["Authorization"] = f"Bearer {key}"
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        )

    @property
    def base_url(self) -> str:
        return self._base

    def sent_prompt(self, request: GenerationRequest) -> str:
        # The negative prompt travels in its own field.
        return request.prompt

    def sent_parameters(self, request: GenerationRequest) -> dict[str, Any]:
        """The generation parameters this wire actually carries."""
        return self._extra_body(request)

    def _extra_body(self, request: GenerationRequest) -> dict[str, Any]:
        extra: dict[str, Any] = {"priority": request.priority}
        if request.seed is not None:
            extra["seed"] = request.seed
        if request.recipe is not None:
            extra["recipe"] = request.recipe
        if request.negative_prompt:
            extra["negative_prompt"] = request.negative_prompt
        return extra

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": self.sent_prompt(request),
            "size": size_for_ratio(request.aspect_ratio),
            "n": 1,
            "response_format": "b64_json",
            "stream": True,
            **self._extra_body(request),
        }
        files = []
        for p in request.reference_images:
            data = read_reference(p)
            files.append(("image[]", (p.name, data, sniff_mime(data))))
        if files:
            url = f"{self._base}/images/edits"
            kwargs: dict[str, Any] = {
                "data": {k: v if isinstance(v, str) else json.dumps(v) for k, v in body.items()},
                "files": files,
            }
        else:
            url = f"{self._base}/images/generations"
            kwargs = {"json": body}
        deadline = time.monotonic() + self._deadline_s
        try:
            with self._http.stream("POST", url, headers=self._headers, **kwargs) as resp:
                if resp.status_code >= 400:
                    raw = resp.read()
                    err = _error_envelope(raw) or {"message": raw[:200].decode("utf-8", "replace")}
                    raise _refusal(err, resp.status_code, resp.headers)
                ctype = resp.headers.get("content-type", "")
                if ctype.startswith("text/event-stream"):
                    items = self._read_stream(resp, deadline)
                else:
                    items = self._read_json(self._read_body(resp, deadline))
        except httpx.TransportError as e:
            if never_sent(e):
                raise ProviderBusy(
                    f"{self.provider_id}: no connection to {self._base} ({e}); the job never started",
                    CONNECT_REFUSED_RETRY_AFTER_S,
                    code="connection_refused",
                ) from e
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: transport dropped after the request was sent: {e}"
            ) from e
        return self._stage(request, staging_dir, items)

    def _past(self, deadline: float) -> None:
        if time.monotonic() > deadline:
            raise ProviderOutcomeUnknown(
                f"{self.provider_id}: still running after the {self._deadline_s:.0f}s deadline; "
                "stopped reading (the host may still finish the job)"
            )

    def _lines(self, resp: httpx.Response, deadline: float) -> Iterator[str]:
        for line in resp.iter_lines():
            self._past(deadline)
            yield line

    def _read_body(self, resp: httpx.Response, deadline: float) -> bytes:
        chunks: list[bytes] = []
        for chunk in resp.iter_bytes():
            self._past(deadline)
            chunks.append(chunk)
        return b"".join(chunks)

    def _read_stream(self, resp: httpx.Response, deadline: float) -> list[dict[str, Any]]:
        for event, data in _sse_events(self._lines(resp, deadline)):
            try:
                payload = json.loads(data)
            except ValueError:
                payload = None
            name = event or (str(payload.get("type")) if isinstance(payload, dict) else "")
            if name == "error":
                err = payload.get("error") if isinstance(payload, dict) else None
                raise _refusal(err if isinstance(err, dict) else {}, None, resp.headers)
            if name in TERMINAL_EVENTS:
                if not isinstance(payload, dict):
                    raise ProviderOutcomeUnknown(f"{name} event carried unreadable data")
                return [payload]
        raise ProviderOutcomeUnknown("the stream ended without a completed or error event")

    def _read_json(self, raw: bytes) -> list[dict[str, Any]]:
        try:
            body = json.loads(raw)
        except ValueError as e:
            raise ProviderOutcomeUnknown(f"unreadable response: {e}") from e
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ProviderOutcomeUnknown("response has no 'data' list")
        return [i for i in items if isinstance(i, dict)]

    def _stage(
        self, request: GenerationRequest, staging_dir: Path, items: list[dict[str, Any]]
    ) -> SubmittedJob:
        staged: list[str] = []
        records: list[dict[str, Any]] = []
        for item in items:
            b64 = item.get("b64_json")
            if not b64:
                continue
            record = item.get("qontinui_provenance")
            gaps = provenance.facade_missing(record)
            if gaps:
                raise PermanentProviderError(
                    f"{self.provider_id}: result carries incomplete qontinui_provenance {gaps}; "
                    "an unprovenanced image is never accepted"
                )
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise PermanentProviderError(f"invalid base64 image: {e}") from e
            staged.append(str(write_staged(staging_dir, f"{request.job_id}-{len(staged)}", data)))
            assert isinstance(record, dict)
            records.append(record)
        if not staged:
            raise PermanentProviderError(f"{self.provider_id}: the answer carried no image")
        return SubmittedJob(
            external_id=request.job_id, data={"staged": staged, "provenance": records}
        )

    def poll(self, job: SubmittedJob) -> PollResult:
        return PollResult(status="done")

    def download(self, job: SubmittedJob, dest_dir: Path) -> list[Path]:
        return copy_staged(job, dest_dir)
