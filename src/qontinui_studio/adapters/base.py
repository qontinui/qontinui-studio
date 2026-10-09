"""The one adapter interface every provider sits behind: submit, poll, download.

Synchronous APIs (Gemini, OpenAI images) answer within the submit call; their
adapters stage the bytes under the job's staging dir at submit time and
``poll`` reports success immediately. An asynchronous provider would return a
pending handle and be polled by the scheduler. Either way the handle is plain
JSON so the scheduler can persist it and resume after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


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
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for src in staged_paths(job):
        dst = dest_dir / src.name
        dst.write_bytes(src.read_bytes())
        out.append(dst)
    return out


EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
