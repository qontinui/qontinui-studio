"""The one adapter interface every provider sits behind: submit, poll, download.

Synchronous APIs (Gemini, OpenAI images) answer within the submit call; their
adapters stage the bytes under the job's staging dir at submit time and
``poll`` reports success immediately. An asynchronous provider would return a
pending handle and be polled by the scheduler. Either way the handle is plain
JSON so the scheduler can persist it and resume after a crash.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from PIL import Image, UnidentifiedImageError

from qontinui_studio.errors import PermanentProviderError, TransientProviderError


@dataclass(frozen=True)
class GenerationRequest:
    job_id: str
    prompt: str
    aspect_ratio: str
    reference_images: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class SubmittedJob:
    """A provider-side job handle. ``data`` must be JSON-serialisable."""

    external_id: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PollResult:
    status: Literal["pending", "succeeded", "failed"]
    error: str | None = None


class ProviderAdapter(Protocol):
    provider_id: str
    model: str
    # Upper bound on how long one submit/poll/download call can block. The
    # scheduler's claim lease is always longer, so a request still in flight
    # is never reclaimed by another process.
    hard_timeout_s: float

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob: ...

    def poll(self, job: SubmittedJob) -> PollResult: ...

    def download(self, job: SubmittedJob, dest_dir: Path) -> list[Path]: ...


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
    """Write ``data`` under ``staging_dir`` with its sniffed extension. A disk
    error is transient (it is local, and retrying after space frees can work)."""
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
