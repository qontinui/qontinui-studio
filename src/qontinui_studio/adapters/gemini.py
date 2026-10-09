"""Gemini image generation over the official API (google-genai).

The model comes from providers.yaml (shipped: ``gemini-3.1-flash-image``,
"Nano Banana 2"; the original ``gemini-2.5-flash-image`` was shut down on
2026-10-02). The key comes from the env var named in providers.yaml
(``GEMINI_API_KEY`` by default).
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image

from qontinui_studio.adapters.base import (
    GenerationRequest,
    PollResult,
    SubmittedJob,
    copy_staged,
    read_reference,
    write_staged,
)
from qontinui_studio.errors import (
    MissingCredentialError,
    PermanentProviderError,
    TransientProviderError,
)


class GeminiImageAdapter:
    def __init__(
        self,
        provider_id: str,
        model: str,
        api_key_env: str = "GEMINI_API_KEY",
        client: genai.Client | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        if client is None:
            key = os.environ.get(api_key_env)
            if not key:
                raise MissingCredentialError(f"{provider_id}: env var {api_key_env} is unset")
            client = genai.Client(api_key=key)
        self._client = client

    def submit(self, request: GenerationRequest, staging_dir: Path) -> SubmittedJob:
        contents: list[str | Image.Image] = [request.prompt]
        for ref in request.reference_images:
            # Decode fully and close the file; the SDK gets an in-memory copy.
            with Image.open(io.BytesIO(read_reference(ref))) as img:
                img.load()
                contents.append(img.copy())
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,  # type: ignore[arg-type]
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio=request.aspect_ratio),
                ),
            )
        except genai_errors.APIError as e:
            if e.code == 429 or e.code >= 500:
                raise TransientProviderError(f"gemini {e.code}: {e.message}") from e
            raise PermanentProviderError(f"gemini {e.code}: {e.message}") from e
        except httpx.TransportError as e:
            raise TransientProviderError(f"gemini transport: {e}") from e
        except (genai_errors.UnknownApiResponseError, ValueError) as e:
            # A 2xx whose body did not parse (truncated JSON, bad base64, schema).
            raise TransientProviderError(f"gemini unreadable response: {e}") from e

        staged: list[str] = []
        for candidate in response.candidates or []:
            if candidate.content is None:
                continue
            for part in candidate.content.parts or []:
                blob = part.inline_data
                if blob is None or not blob.data:
                    continue
                path = write_staged(staging_dir, f"{request.job_id}-{len(staged)}", blob.data)
                staged.append(str(path))
        if not staged:
            reason = ""
            if response.prompt_feedback is not None and response.prompt_feedback.block_reason:
                reason = f" (blocked: {response.prompt_feedback.block_reason})"
            raise PermanentProviderError(f"gemini returned no image{reason}")
        return SubmittedJob(
            external_id=response.response_id or request.job_id, data={"staged": staged}
        )

    def poll(self, job: SubmittedJob) -> PollResult:
        return PollResult(status="succeeded")

    def download(self, job: SubmittedJob, dest_dir: Path) -> list[Path]:
        return copy_staged(job, dest_dir)
