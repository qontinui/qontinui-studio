"""API adapters against mocked HTTP (respx). No test makes a live call."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from qontinui_studio.adapters.base import GenerationRequest
from qontinui_studio.adapters.gemini import GeminiImageAdapter
from qontinui_studio.adapters.openai_images import OpenAIImageAdapter, size_for_ratio
from qontinui_studio.errors import (
    MissingCredentialError,
    PermanentProviderError,
    TransientProviderError,
)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent"
)


def _png(size: tuple[int, int] = (64, 36)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def gemini(monkeypatch: pytest.MonkeyPatch) -> GeminiImageAdapter:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return GeminiImageAdapter("gemini", "gemini-3.1-flash-image")


def _req(**kw: object) -> GenerationRequest:
    base: dict[str, object] = {"job_id": "job1", "prompt": "a cafe", "aspect_ratio": "16:9"}
    base.update(kw)
    return GenerationRequest(**base)  # type: ignore[arg-type]


@respx.mock
def test_gemini_submit_poll_download(gemini: GeminiImageAdapter, tmp_path: Path) -> None:
    route = respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "responseId": "resp-1",
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(_png()).decode(),
                                    }
                                }
                            ],
                        }
                    }
                ],
            },
        )
    )
    handle = gemini.submit(_req(), tmp_path / "staging")
    assert handle.external_id == "resp-1"
    sent = json.loads(route.calls.last.request.content)
    assert sent["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert sent["generationConfig"]["imageConfig"]["aspectRatio"] == "16:9"
    assert route.calls.last.request.headers["x-goog-api-key"] == "test-key"
    assert gemini.poll(handle).status == "succeeded"
    files = gemini.download(handle, tmp_path / "out")
    assert len(files) == 1
    with Image.open(files[0]) as img:
        assert img.size == (64, 36)


@respx.mock
@pytest.mark.parametrize(
    ("status", "error"),
    [(429, TransientProviderError), (503, TransientProviderError), (400, PermanentProviderError)],
)
def test_gemini_error_mapping(
    gemini: GeminiImageAdapter, tmp_path: Path, status: int, error: type[Exception]
) -> None:
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            status, json={"error": {"code": status, "message": "nope", "status": "X"}}
        )
    )
    with pytest.raises(error):
        gemini.submit(_req(), tmp_path)


@respx.mock
def test_gemini_no_image_is_permanent(gemini: GeminiImageAdapter, tmp_path: Path) -> None:
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": "sorry"}]}}],
                "promptFeedback": {"blockReason": "SAFETY"},
            },
        )
    )
    with pytest.raises(PermanentProviderError, match="no image"):
        gemini.submit(_req(), tmp_path)


def test_gemini_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(MissingCredentialError):
        GeminiImageAdapter("gemini", "m")


@pytest.fixture
def openai(monkeypatch: pytest.MonkeyPatch) -> OpenAIImageAdapter:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return OpenAIImageAdapter("openai", "gpt-image-1")


@respx.mock
def test_openai_generations(openai: OpenAIImageAdapter, tmp_path: Path) -> None:
    route = respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={"created": 1760000000, "data": [{"b64_json": base64.b64encode(_png()).decode()}]},
        )
    )
    handle = openai.submit(_req(), tmp_path / "s")
    body = json.loads(route.calls.last.request.content)
    assert body == {"model": "gpt-image-1", "prompt": "a cafe", "size": "1536x1024", "n": 1}
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"
    assert openai.poll(handle).status == "succeeded"
    assert len(openai.download(handle, tmp_path / "o")) == 1


@respx.mock
def test_openai_edits_with_reference_images(openai: OpenAIImageAdapter, tmp_path: Path) -> None:
    ref = tmp_path / "ref.png"
    ref.write_bytes(_png())
    route = respx.post("https://api.openai.com/v1/images/edits").mock(
        return_value=httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]}
        )
    )
    openai.submit(_req(reference_images=[ref], aspect_ratio="9:16"), tmp_path / "s")
    content = route.calls.last.request.content
    assert b'name="image[]"' in content and b"1024x1536" in content


@respx.mock
def test_openai_error_mapping(openai: OpenAIImageAdapter, tmp_path: Path) -> None:
    url = "https://api.openai.com/v1/images/generations"
    respx.post(url).mock(
        return_value=httpx.Response(
            429, headers={"retry-after": "20"}, json={"error": {"message": "slow"}}
        )
    )
    with pytest.raises(TransientProviderError) as info:
        openai.submit(_req(), tmp_path)
    assert info.value.retry_after_s == 20.0
    respx.post(url).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad prompt"}})
    )
    with pytest.raises(PermanentProviderError, match="bad prompt"):
        openai.submit(_req(), tmp_path)
    respx.post(url).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TransientProviderError):
        openai.submit(_req(), tmp_path)


def test_openai_size_mapping() -> None:
    assert size_for_ratio("1:1") == "1024x1024"
    assert size_for_ratio("16:9") == "1536x1024"
    assert size_for_ratio("2:3") == "1024x1536"


def test_openai_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingCredentialError):
        OpenAIImageAdapter("openai", "gpt-image-1")


def _gemini_body(data: bytes, mime: str = "image/png") -> dict[str, object]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}
                    ],
                }
            }
        ]
    }


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG")
    return buf.getvalue()


@respx.mock
def test_gemini_extension_comes_from_the_bytes_not_the_declared_mime(
    gemini: GeminiImageAdapter, tmp_path: Path
) -> None:
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=_gemini_body(_jpeg())))
    handle = gemini.submit(_req(), tmp_path / "s")
    [path] = gemini.download(handle, tmp_path / "o")
    assert path.suffix == ".jpg"


@respx.mock
def test_gemini_non_image_bytes_are_permanent(gemini: GeminiImageAdapter, tmp_path: Path) -> None:
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(200, json=_gemini_body(b"<html>nope</html>"))
    )
    with pytest.raises(PermanentProviderError, match="not an image"):
        gemini.submit(_req(), tmp_path)


@respx.mock
def test_gemini_unparseable_2xx_is_transient(gemini: GeminiImageAdapter, tmp_path: Path) -> None:
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, content=b"{truncated"))
    with pytest.raises(TransientProviderError):
        gemini.submit(_req(), tmp_path)


def test_unreadable_reference_image_is_permanent(
    gemini: GeminiImageAdapter, openai: OpenAIImageAdapter, tmp_path: Path
) -> None:
    bad = tmp_path / "ref.png"
    bad.write_bytes(b"not an image")
    for adapter in (gemini, openai):
        with pytest.raises(PermanentProviderError):
            adapter.submit(_req(reference_images=[bad]), tmp_path)
        with pytest.raises(PermanentProviderError):
            adapter.submit(_req(reference_images=[tmp_path / "missing.png"]), tmp_path)


@respx.mock
def test_openai_reference_mime_is_sniffed(openai: OpenAIImageAdapter, tmp_path: Path) -> None:
    ref = tmp_path / "ref.png"  # misleading name: the bytes are JPEG
    ref.write_bytes(_jpeg())
    route = respx.post("https://api.openai.com/v1/images/edits").mock(
        return_value=httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]}
        )
    )
    openai.submit(_req(reference_images=[ref]), tmp_path / "s")
    assert b"Content-Type: image/jpeg" in route.calls.last.request.content


@respx.mock
def test_openai_bad_bodies(openai: OpenAIImageAdapter, tmp_path: Path) -> None:
    url = "https://api.openai.com/v1/images/generations"
    respx.post(url).mock(return_value=httpx.Response(200, content=b"<html>gateway</html>"))
    with pytest.raises(TransientProviderError, match="unreadable"):
        openai.submit(_req(), tmp_path)
    respx.post(url).mock(return_value=httpx.Response(200, json={"data": [{"b64_json": "!!!"}]}))
    with pytest.raises(PermanentProviderError, match="base64"):
        openai.submit(_req(), tmp_path)
    respx.post(url).mock(
        return_value=httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(b"text").decode()}]}
        )
    )
    with pytest.raises(PermanentProviderError, match="not an image"):
        openai.submit(_req(), tmp_path)
