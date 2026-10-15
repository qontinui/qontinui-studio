"""The one adapter interface every provider sits behind: submit, poll, download.

Synchronous APIs (Gemini, OpenAI images, the local facade) answer within the
submit call; their adapters stage the bytes under the job's staging dir at
submit time and ``poll`` reports ``done`` immediately. An asynchronous provider
returns a ``queued``/``running`` handle and is polled by the scheduler. Either
way the handle is plain JSON so the scheduler can persist it and resume after a
crash.

Job states (plan 2026-10-13 Phase 11 step 1): ``queued | running | done |
failed | unknown``, plus the host's typed refusals ``deferred | busy`` (each
with ``retry_after_s``). ``unknown`` — the transport failed mid-flight, or the
job id is not found — is never collapsed into ``failed`` or ``done``. A
synchronous adapter meets the refusals and ``unknown`` inside ``submit``, so
they are also typed exceptions (``ProviderBusy``, ``ProviderDeferred``,
``ProviderOutcomeUnknown``).

Every byte an adapter stages goes through :func:`write_staged`, which writes
the IPTC AI-generated marker into it (``marker.mark_bytes``).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from PIL import Image, UnidentifiedImageError

from qontinui_studio import marker
from qontinui_studio.errors import PermanentProviderError, TransientProviderError

Priority = Literal["interactive", "bulk"]


@dataclass(frozen=True)
class GenerationRequest:
    job_id: str
    # The brief's prompt plus its style guide. The negative prompt is NOT in
    # it: an adapter whose wire has a negative prompt sends it there; one
    # without folds it in as ``Avoid: ...`` text (``fold_negative``).
    prompt: str
    aspect_ratio: str
    reference_images: list[Path] = field(default_factory=list)
    negative_prompt: str | None = None
    seed: int | None = None
    # A recipe id on the host (e.g. ``hero``); the model is the provider row's.
    recipe: str | None = None
    # ``bulk`` work is deferred by the host outside its bulk window and never
    # spills onto another provider when refused.
    priority: Priority = "interactive"


def fold_negative(request: GenerationRequest) -> str:
    """The prompt for a wire with no negative-prompt field."""
    if not request.negative_prompt:
        return request.prompt
    return f"{request.prompt}\n\nAvoid: {request.negative_prompt.strip()}"


@dataclass(frozen=True)
class SubmittedJob:
    """A provider-side job handle. ``data`` must be JSON-serialisable."""

    external_id: str
    data: dict[str, Any] = field(default_factory=dict)


JobStatus = Literal["queued", "running", "done", "failed", "unknown", "deferred", "busy"]


@dataclass(frozen=True)
class PollResult:
    status: JobStatus
    error: str | None = None
    # Set with ``deferred``/``busy``: when the host says to come back.
    retry_after_s: float | None = None


class ProviderAdapter(Protocol):
    provider_id: str
    model: str
    # The providers.yaml adapter kind (recorded in every provenance sidecar).
    kind: str
    # Upper bound on how long one submit/poll/download call can block. The
    # scheduler's claim lease is always longer, so a request still in flight
    # is never reclaimed by another process.
    hard_timeout_s: float

    def sent_prompt(self, request: GenerationRequest) -> str:
        """Exactly the prompt text this adapter puts on the wire (D5 records it)."""
        ...

    def sent_parameters(self, request: GenerationRequest) -> dict[str, Any]:
        """The generation parameters this wire actually carries (seed, recipe,
        negative prompt ...); provenance records only these."""
        ...

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob: ...

    def poll(self, job: SubmittedJob) -> PollResult: ...

    def download(self, job: SubmittedJob, dest_dir: Path) -> list[Path]: ...


_DAILY_QUOTA = re.compile(r"insufficient_quota|per[ _-]?day|daily", re.IGNORECASE)


def is_daily_quota(text: str) -> bool:
    """Whether a provider's 429 text says the quota for the PERIOD is spent
    (Gemini ``...PerDay...`` quota ids, OpenAI ``insufficient_quota``), as
    opposed to a per-minute rate limit that a short backoff clears."""
    return bool(_DAILY_QUOTA.search(text))


def never_sent(e: httpx.TransportError) -> bool:
    """Whether a transport error happened BEFORE the request reached the host
    (no connection, or none from the pool), so the job provably never started.
    Every other transport error is a drop after the request was sent."""
    return isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


def staged_provenance(job: SubmittedJob) -> list[dict[str, Any] | None]:
    """The host's per-file provenance records, aligned with ``staged_paths``
    (``None`` where the provider returned none)."""
    raw = job.data.get("provenance")
    files = staged_paths(job)
    if not isinstance(raw, list):
        return [None] * len(files)
    out = [r if isinstance(r, dict) else None for r in raw]
    return (out + [None] * len(files))[: len(files)]


def staged_paths(job: SubmittedJob) -> list[Path]:
    raw = job.data.get("staged", [])
    if not isinstance(raw, list):
        return []
    return [Path(str(p)) for p in raw]


def copy_staged(job: SubmittedJob, dest_dir: Path) -> list[Path]:
    """``download`` for adapters that staged their bytes at submit time."""
    out: list[Path] = []
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src in staged_paths(job):
            dst = dest_dir / src.name
            dst.write_bytes(src.read_bytes())
            out.append(dst)
    except FileNotFoundError as e:
        # The staged bytes are gone (e.g. .studio/staging was cleaned): the
        # provider job must be redone.
        raise PermanentProviderError(f"staged file missing: {e}") from e
    except OSError as e:
        raise TransientProviderError(f"cannot copy staged file: {e}") from e
    return out


_EXT_BY_PIL_FORMAT = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "WEBP": ".webp",
    "GIF": ".gif",
    "AVIF": ".avif",
}
_MIME_BY_PIL_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "AVIF": "image/avif",
}


def _pil_format(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
            fmt = img.format or ""
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as e:
        raise PermanentProviderError(f"provider returned bytes that are not an image: {e}") from e
    if fmt not in _EXT_BY_PIL_FORMAT:
        raise PermanentProviderError(f"provider returned an unsupported image format '{fmt}'")
    return fmt


def sniff_extension(data: bytes) -> str:
    """The file extension for image ``data``, from its actual bytes — never from
    a provider's declared MIME type. Non-image bytes are a permanent failure."""
    return _EXT_BY_PIL_FORMAT[_pil_format(data)]


def sniff_mime(data: bytes) -> str:
    return _MIME_BY_PIL_FORMAT[_pil_format(data)]


def write_staged(staging_dir: Path, name: str, data: bytes) -> Path:
    """Write ``data`` under ``staging_dir`` with its sniffed extension, carrying
    the IPTC AI-generated marker. A disk error is transient (it is local, and
    retrying after space frees can work)."""
    sniff_extension(data)  # non-image bytes are a permanent failure, before marking
    try:
        data = marker.mark_bytes(data)
    except marker.MarkerError as e:
        raise PermanentProviderError(f"cannot mark provider bytes: {e}") from e
    path = staging_dir / f"{name}{sniff_extension(data)}"
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as e:
        raise TransientProviderError(f"cannot write {path}: {e}") from e
    return path


def read_reference(path: Path) -> bytes:
    """Read a brief's reference image; an unreadable one is permanent (config)."""
    try:
        data = path.read_bytes()
    except OSError as e:
        raise PermanentProviderError(f"cannot read reference image {path}: {e}") from e
    _pil_format(data)
    return data
