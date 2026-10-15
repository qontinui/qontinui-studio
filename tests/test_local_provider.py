"""The local OpenAI-compatible image provider against a fake host on a real
loopback socket (plan 2026-10-13 Phase 11 steps 1, 2 and 6): every typed state
and refusal, a real transport drop mapping to ``unknown``, and the facade's
provenance parsed and enforced."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from qontinui_studio import marker
from qontinui_studio.adapters.base import GenerationRequest, fold_negative, staged_provenance
from qontinui_studio.adapters.openai_compatible_images import (
    OpenAICompatibleImageAdapter,
    size_for_ratio,
)
from qontinui_studio.errors import (
    PermanentProviderError,
    ProviderBusy,
    ProviderDeferred,
    ProviderDisabled,
    ProviderOutcomeUnknown,
)

from . import imagehost
from .imagehost import ImageHost

pytestmark = pytest.mark.local_http


@pytest.fixture
def host() -> Iterator[ImageHost]:
    h = imagehost.start(ImageHost())
    yield h
    imagehost.stop(h)


@pytest.fixture
def adapter(host: ImageHost) -> OpenAICompatibleImageAdapter:
    return OpenAICompatibleImageAdapter("local-gpu", "z-image-turbo", host.base_url)


def _req(**kw: object) -> GenerationRequest:
    base: dict[str, object] = {
        "job_id": "job1",
        "prompt": "an empty cafe",
        "aspect_ratio": "16:9",
        "negative_prompt": "text, logos",
        "seed": 1234,
        "recipe": "hero",
    }
    base.update(kw)
    return GenerationRequest(**base)  # type: ignore[arg-type]


def test_sse_generation_sends_the_extra_body_and_stages_a_marked_provenanced_file(
    host: ImageHost, adapter: OpenAICompatibleImageAdapter, tmp_path: Path
) -> None:
    host.queue(imagehost.sse_reply(*imagehost.progress(), imagehost.completed()))
    handle = adapter.submit(_req(), tmp_path / "s")
    sent = host.received[-1]
    assert sent.path == "/v1/images/generations"
    assert sent.json is not None
    # The negative prompt rides in its own field — never folded into the prompt.
    assert sent.json["prompt"] == "an empty cafe" and "Avoid" not in sent.json["prompt"]
    assert sent.json["negative_prompt"] == "text, logos"
    assert sent.json["seed"] == 1234 and sent.json["recipe"] == "hero"
    assert sent.json["model"] == "z-image-turbo" and sent.json["stream"] is True
    assert sent.json["priority"] == "interactive"
    assert sent.json["size"] == size_for_ratio("16:9") == "1344x768"
    assert "authorization" not in {k.lower() for k in sent.headers}
    assert adapter.poll(handle).status == "done"
    [path] = adapter.download(handle, tmp_path / "o")
    assert marker.is_marked(path)
    [record] = staged_provenance(handle)
    assert record is not None and record["spdx"] == "Apache-2.0" and record["seed"] == 1234


def test_the_marker_is_written_even_when_the_host_sends_unmarked_bytes(
    host: ImageHost, adapter: OpenAICompatibleImageAdapter, tmp_path: Path
) -> None:
    host.queue(imagehost.sse_reply(imagehost.completed(b64_json=imagehost.png_b64(marked=False))))
    handle = adapter.submit(_req(), tmp_path / "s")
    [path] = adapter.download(handle, tmp_path / "o")
    assert marker.is_marked(path)


def test_non_streamed_json_answer(
    host: ImageHost, adapter: OpenAICompatibleImageAdapter, tmp_path: Path
) -> None:
    host.queue(
        imagehost.json_reply(
            200,
            {
                "created": 1,
                "data": [
                    {
                        "b64_json": imagehost.png_b64(),
                        "qontinui_provenance": imagehost.facade_provenance(),
                    }
                ],
            },
        )
    )
    handle = adapter.submit(_req(), tmp_path / "s")
    assert len(adapter.download(handle, tmp_path / "o")) == 1


def test_edits_go_multipart_with_the_alias_in_the_form(
    host: ImageHost, adapter: OpenAICompatibleImageAdapter, tmp_path: Path
) -> None:
    ref = tmp_path / "ref.png"
    Image.new("RGB", (8, 8)).save(ref)
    host.queue(imagehost.sse_reply(imagehost.completed("image_edit.completed")))
    adapter.submit(_req(reference_images=[ref]), tmp_path / "s")
    sent = host.received[-1]
    assert sent.path == "/v1/images/edits"
    assert sent.form["model"] == "z-image-turbo" and sent.form["recipe"] == "hero"
    assert sent.form["seed"] == "1234" and sent.files == ["image[]"]


@pytest.mark.parametrize(
    ("reply", "exc", "retry"),
    [
        (imagehost.refusal(503, "busy", 42, vram_held_by="out_of_band"), ProviderBusy, 42.0),
        (imagehost.refusal(429, "deferred", 3600), ProviderDeferred, 3600.0),
        # llama-swap's own concurrency rejection is a busy, read from error.type.
        (
            imagehost.json_reply(
                429,
                {"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}},
                {"Retry-After": "7"},
            ),
            ProviderBusy,
            7.0,
        ),
    ],
    ids=["busy", "deferred", "llama-swap-429"],
)
def test_typed_refusals_carry_retry_after(
    host: ImageHost,
    adapter: OpenAICompatibleImageAdapter,
    tmp_path: Path,
    reply: imagehost.Reply,
    exc: type[Exception],
    retry: float,
) -> None:
    host.queue(reply)
    with pytest.raises(exc) as info:
        adapter.submit(_req(), tmp_path)
    assert info.value.retry_after_s == retry  # type: ignore[attr-defined]


def test_busy_names_who_holds_the_vram(
    host: ImageHost, adapter: OpenAICompatibleImageAdapter, tmp_path: Path
) -> None:
    host.queue(imagehost.refusal(503, "busy", 30, vram_held_by="out_of_band"))
    with pytest.raises(ProviderBusy) as info:
        adapter.submit(_req(), tmp_path)
    assert info.value.detail == {"vram_held_by": "out_of_band"}


def test_a_refusal_in_the_stream_is_typed_too(
    host: ImageHost, adapter: OpenAICompatibleImageAdapter, tmp_path: Path
) -> None:
    error = {"type": "error", "error": {"type": "busy", "retry_after_s": 15}}
    host.queue(imagehost.sse_reply(imagehost.sse_event("error", error)))
    with pytest.raises(ProviderBusy) as info:
        adapter.submit(_req(), tmp_path)
    assert info.value.retry_after_s == 15.0


@pytest.mark.parametrize(
    ("reply", "exc", "match"),
    [
        (imagehost.refusal(503, "disabled"), ProviderDisabled, "disabled"),
        (imagehost.refusal(500, "weights_unverified"), PermanentProviderError, "weights"),
        (imagehost.refusal(400, "invalid_request_error"), PermanentProviderError, "failed"),
    ],
    ids=["disabled", "weights-unverified", "bad-request"],
)
def test_permanent_answers(
    host: ImageHost,
    adapter: OpenAICompatibleImageAdapter,
    tmp_path: Path,
    reply: imagehost.Reply,
    exc: type[Exception],
    match: str,
) -> None:
    host.queue(reply)
    with pytest.raises(exc, match=match):
        adapter.submit(_req(), tmp_path)


def test_a_refused_connection_is_busy_because_the_job_never_started(tmp_path: Path) -> None:
    port = imagehost.closed_port()
    local = OpenAICompatibleImageAdapter("local-gpu", "z", f"http://127.0.0.1:{port}/v1")
    with pytest.raises(ProviderBusy) as info:
        local.submit(_req(), tmp_path)
    assert info.value.code == "connection_refused"


def test_a_transport_drop_after_the_request_was_sent_is_unknown(
    host: ImageHost, adapter: OpenAICompatibleImageAdapter, tmp_path: Path
) -> None:
    """A real dropped socket mid-stream: the job may have run. ``unknown``,
    never ``failed`` (and never ``busy``)."""
    host.queue(imagehost.sse_reply(*imagehost.progress(), drop_after=2))
    with pytest.raises(ProviderOutcomeUnknown, match="dropped after the request was sent"):
        adapter.submit(_req(), tmp_path)
    assert host.received, "the request reached the host"


@pytest.mark.parametrize(
    "reply",
    [
        imagehost.sse_reply(*imagehost.progress()),  # ends with no terminal event
        imagehost.raw_reply(502, b"<html>bad gateway</html>", "text/html"),  # untyped 5xx
        imagehost.raw_reply(200, b'{"data": [', "application/json"),  # truncated 2xx
    ],
    ids=["no-terminal-event", "untyped-5xx", "truncated-2xx"],
)
def test_lost_results_are_unknown(
    host: ImageHost, adapter: OpenAICompatibleImageAdapter, tmp_path: Path, reply: imagehost.Reply
) -> None:
    host.queue(reply)
    with pytest.raises(ProviderOutcomeUnknown):
        adapter.submit(_req(), tmp_path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"spdx": None},
        {"spdx": "GPL-3.0-only"},
        {"recipe": {"id": "hero"}},
        {"models": [{"id": "z", "weights": []}]},
        {"seed": None},
    ],
    ids=["no-spdx", "gpl", "no-recipe-hash", "no-weight-hash", "no-seed"],
)
def test_incomplete_facade_provenance_is_refused(
    host: ImageHost,
    adapter: OpenAICompatibleImageAdapter,
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    record = imagehost.facade_provenance(**overrides)
    host.queue(imagehost.sse_reply(imagehost.completed(qontinui_provenance=record)))
    with pytest.raises(PermanentProviderError, match="incomplete qontinui_provenance"):
        adapter.submit(_req(), tmp_path / "s")
    assert not (tmp_path / "s").exists() or not any((tmp_path / "s").iterdir())


def test_cloud_wires_fold_the_negative_prompt_the_local_wire_does_not(
    adapter: OpenAICompatibleImageAdapter,
) -> None:
    req = _req()
    assert adapter.sent_prompt(req) == "an empty cafe"
    assert fold_negative(req) == "an empty cafe\n\nAvoid: text, logos"
    assert fold_negative(_req(negative_prompt=None)) == "an empty cafe"


def test_an_optional_api_key_is_sent_as_a_bearer(
    host: ImageHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_GPU_KEY", "k-test")
    keyed = OpenAICompatibleImageAdapter("local-gpu", "z", host.base_url, "LOCAL_GPU_KEY")
    host.queue(imagehost.sse_reply(imagehost.completed()))
    keyed.submit(_req(), tmp_path)
    assert host.received[-1].headers.get("Authorization") == "Bearer k-test"
