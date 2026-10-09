"""OpenAI Images API (``gpt-image-1``) over plain HTTPS.

``POST /v1/images/generations`` for a text prompt, ``POST /v1/images/edits``
when the brief carries reference images. The key comes from the env var named
in providers.yaml (``OPENAI_API_KEY`` by default). The Images API is billed, not
free — the providers.yaml row carries no ``daily_quota`` for it, so the
scheduler assigns it nothing until the operator sets one.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import Any

import httpx

from qontinui_studio.adapters.base import (
    GenerationRequest,
    PollResult,
    SubmittedJob,
    copy_staged,
    read_reference,
    sniff_mime,
    write_staged,
)
from qontinui_studio.errors import (
    MissingCredentialError,
    PermanentProviderError,
    TransientProviderError,
)

API_BASE = "https://api.openai.com/v1"
# gpt-image-1 accepts exactly these sizes.
SIZES: list[tuple[int, int]] = [(1024, 1024), (1536, 1024), (1024, 1536)]


def size_for_ratio(aspect_ratio: str) -> str:
    w, h = (int(x) for x in aspect_ratio.split(":"))
    target = w / h
    best = min(SIZES, key=lambda s: abs(s[0] / s[1] - target))
    return f"{best[0]}x{best[1]}"


class OpenAIImageAdapter:
    def __init__(
        self,
        provider_id: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        http: httpx.Client | None = None,
        api_base: str = API_BASE,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        # httpx timeouts are per phase (connect/write/read), not total: bound a
        # call by all three plus headroom.
        self.hard_timeout_s = 600.0
        key = os.environ.get(api_key_env)
        if not key:
            raise MissingCredentialError(f"{provider_id}: env var {api_key_env} is unset")
        self._headers = {"Authorization": f"Bearer {key}"}
        self._http = http or httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0))
        self._base = api_base.rstrip("/")

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        size = size_for_ratio(request.aspect_ratio)
        files = []
        for p in request.reference_images:
            data = read_reference(p)
            files.append(("image[]", (p.name, data, sniff_mime(data))))
        try:
            if files:
                resp = self._http.post(
                    f"{self._base}/images/edits",
                    headers=self._headers,
                    data={"model": self.model, "prompt": request.prompt, "size": size, "n": "1"},
                    files=files,
                )
            else:
                resp = self._http.post(
                    f"{self._base}/images/generations",
                    headers=self._headers,
                    json={"model": self.model, "prompt": request.prompt, "size": size, "n": 1},
                )
        except httpx.TransportError as e:
            raise TransientProviderError(f"openai transport: {e}") from e

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("retry-after")
            raise TransientProviderError(
                f"openai {resp.status_code}: {_error_message(resp)}",
                retry_after_s=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if resp.status_code >= 400:
            raise PermanentProviderError(f"openai {resp.status_code}: {_error_message(resp)}")

        try:
            body = resp.json()
        except ValueError as e:
            # A 2xx with a truncated/non-JSON body (proxy hiccup): worth a retry.
            raise TransientProviderError(f"openai unreadable response: {e}") from e
        if not isinstance(body, dict):
            raise TransientProviderError("openai response is not a JSON object")
        items: Any = body.get("data") or []
        if not isinstance(items, list):
            raise PermanentProviderError("openai response 'data' is not a list")
        staged: list[str] = []
        for i, item in enumerate(items):
            b64 = item.get("b64_json") if isinstance(item, dict) else None
            if not b64:
                continue
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise PermanentProviderError(f"openai returned invalid base64: {e}") from e
            staged.append(str(write_staged(staging_dir, f"{request.job_id}-{i}", data)))
        if not staged:
            raise PermanentProviderError("openai returned no image data")
        return SubmittedJob(
            external_id=str(body.get("created", request.job_id)), data={"staged": staged}
        )

    def poll(self, job: SubmittedJob) -> PollResult:
        return PollResult(status="succeeded")

    def download(self, job: SubmittedJob, dest_dir: Path) -> list[Path]:
        return copy_staged(job, dest_dir)


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        if isinstance(err, dict):
            return str(err.get("message", resp.text[:200]))
    except ValueError:
        pass
    return resp.text[:200]
