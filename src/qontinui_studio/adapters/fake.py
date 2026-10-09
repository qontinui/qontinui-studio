"""An offline adapter that draws a deterministic image locally.

It exists for dry runs and the end-to-end test: it exercises the real
submit/poll/download contract (including a pending first poll) with no network.
It is selected only by a providers.yaml row whose ``adapter`` is ``fake``; the
shipped providers.yaml has none.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw

from qontinui_studio.adapters.base import (
    GenerationRequest,
    PollResult,
    SubmittedJob,
    copy_staged,
)
from qontinui_studio.errors import TransientProviderError

LONG_EDGE = 1600


class FakeImageAdapter:
    def __init__(
        self,
        provider_id: str = "fake",
        model: str = "fake-v1",
        fail_first: int = 0,
        pending_polls: int = 0,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self._fail_remaining = fail_first
        self._pending_polls = pending_polls
        self._polls: dict[str, int] = {}
        self.submitted: list[GenerationRequest] = []

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise TransientProviderError("fake transient failure", retry_after_s=1.0)
        self.submitted.append(request)
        w, h = (int(x) for x in request.aspect_ratio.split(":"))
        if w >= h:
            size = (LONG_EDGE, max(1, round(LONG_EDGE * h / w)))
        else:
            size = (max(1, round(LONG_EDGE * w / h)), LONG_EDGE)
        digest = hashlib.sha256(request.prompt.encode()).digest()
        img = Image.new("RGB", size, (digest[0], digest[1], digest[2]))
        draw = ImageDraw.Draw(img)
        draw.ellipse(
            (size[0] // 4, size[1] // 4, 3 * size[0] // 4, 3 * size[1] // 4),
            fill=(digest[3], digest[4], digest[5]),
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        path = staging_dir / f"{request.job_id}-0.png"
        img.save(path, "PNG")
        return SubmittedJob(external_id=f"fake-{request.job_id}", data={"staged": [str(path)]})

    def poll(self, job: SubmittedJob) -> PollResult:
        # The first ``pending_polls`` polls report pending, which exercises the
        # scheduler's poll loop the way an asynchronous provider would.
        n = self._polls.get(job.external_id, 0)
        self._polls[job.external_id] = n + 1
        return PollResult(status="pending" if n < self._pending_polls else "succeeded")

    def download(self, job: SubmittedJob, dest_dir: Path) -> list[Path]:
        return copy_staged(job, dest_dir)
