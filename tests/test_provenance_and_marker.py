"""Every file the studio writes carries the IPTC AI-generated marker and a
complete provenance sidecar — per provider, cloud included (plan 2026-10-13
Phase 11 steps 4 and 6; decision record a5ec7670)."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from qontinui_studio import marker, provenance
from qontinui_studio.adapters import AdapterSet
from qontinui_studio.adapters.gemini import GeminiImageAdapter
from qontinui_studio.adapters.openai_compatible_images import OpenAICompatibleImageAdapter
from qontinui_studio.adapters.openai_images import OpenAIImageAdapter
from qontinui_studio.briefs import load_brief
from qontinui_studio.pipeline import Context, drive_batch, new_batch
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.providers import load_providers

from . import imagehost
from .imagehost import ImageHost

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent"
)
OPENAI_URL = "https://api.openai.com/v1/images/generations"

ROWS = """
  - id: gemini
    provider: Gemini test row
    plan: test
    api_available: {{value: "yes", source: "https://gemini.test/api"}}
    automation_allowed: {{value: "yes", source: "https://gemini.test/tos"}}
    commercial_use: {{value: "yes", source: "https://gemini.test/tos"}}
    watermark: SynthID (invisible)
    adapter: gemini_api
    model: gemini-3.1-flash-image
    api_key_env: GEMINI_API_KEY
    daily_quota: 10
    tos_reviewed: 2026-10-09
    domains: [gemini.test]
  - id: openai
    provider: OpenAI test row
    plan: test
    api_available: {{value: "yes", source: "https://openai.test/api"}}
    automation_allowed: {{value: "yes", source: "https://openai.test/tos"}}
    commercial_use: {{value: "yes", source: "https://openai.test/tos"}}
    watermark: C2PA metadata
    adapter: openai_images
    model: gpt-image-1
    api_key_env: OPENAI_API_KEY
    daily_quota: 10
    tos_reviewed: 2026-10-09
    domains: [openai.test]
  - id: local-gpu
    provider: Local image facade
    plan: self-hosted
    api_available: {{value: "yes", source: "https://local.test/api"}}
    automation_allowed: {{value: "yes", source: "https://local.test/api"}}
    commercial_use: {{value: "yes", source: "https://local.test/licence"}}
    watermark: IPTC trainedAlgorithmicMedia (XMP)
    watermark_visible: false
    adapter: openai_compatible_images
    model: z-image-turbo
    base_url: {base}
    capacity: unmetered
    tos_reviewed: 2026-10-15
    domains: [local.test]
"""

BRIEF_EXTRA = "negative_prompt: text, logos\nseed: 7\nrecipe: hero\n"


def _png(marked: bool = False) -> bytes:
    buf = io.BytesIO()
    kwargs = marker.save_options("png") if marked else {}
    Image.new("RGB", (64, 36), (200, 120, 40)).save(buf, "PNG", **kwargs)
    return buf.getvalue()


@pytest.fixture
def host() -> Iterator[ImageHost]:
    h = imagehost.start(ImageHost())
    yield h
    imagehost.stop(h)


def _written_images(root: Path) -> list[Path]:
    studio = root / ".studio"
    return sorted(
        p
        for p in studio.rglob("*")
        if p.is_file()
        and p.suffix in (".png", ".webp", ".avif", ".jpg")
        and "staging" not in p.parts
    )


def _run(
    provider: str,
    adapters: dict[str, object],
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
) -> Context:
    path = write_brief(providers=provider)
    path.write_text(path.read_text() + BRIEF_EXTRA)
    ctx = make_context(adapters=AdapterSet(adapters=adapters))  # type: ignore[arg-type]
    out = drive_batch(ctx, new_batch(ctx, load_brief(path)), publish=False)
    assert out.status == "generated", out
    return ctx


def _check_every_file(root: Path, adapter_kind: str, facade: bool) -> int:
    files = _written_images(root)
    # One raw file plus five derivatives (cafe webp+avif, barber webp+avif,
    # the campaign webp).
    assert len(files) == 6, files
    for f in files:
        assert marker.is_marked(f), f"{f} lacks the AI-generated marker"
        record = provenance.read(f)
        assert record is not None, f"{f} has no provenance sidecar"
        assert provenance.missing(record) == [], (f, provenance.missing(record))
        assert record["file"]["sha256"] == sha256_file(f)
        assert record["provider"]["adapter"] == adapter_kind
        assert record["negative_prompt"] == "text, logos" and record["recipe"] == "hero"
        assert record["seed"] == 7
        assert record["c2pa"]["signed"] is False
        assert ("qontinui_provenance" in record) is facade
        if record["file"]["role"] == "derivative":
            assert len(record["file"]["derived_from"]) == 64
    return len(files)


@pytest.mark.local_http
def test_local_provider_end_to_end_every_file_is_marked_and_provenanced(
    host: ImageHost,
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + ROWS.format(base=host.v1_url))
    host.queue(imagehost.sse_reply(*imagehost.progress(), imagehost.completed()))
    local = OpenAICompatibleImageAdapter("local-gpu", "z-image-turbo", host.v1_url)
    ctx = _run("local-gpu", {"local-gpu": local}, studio_root, make_context, write_brief)
    assert _check_every_file(studio_root, "openai_compatible_images", facade=True) == 6
    # The facade's record is embedded VERBATIM, and the prompt carries no Avoid.
    raw = next(f for f in _written_images(studio_root) if "raw" in f.parts)
    record = provenance.read(raw)
    assert record is not None
    assert record["qontinui_provenance"] == imagehost.facade_provenance()
    assert "Avoid" not in record["prompt"]
    row = ctx.conn.execute("SELECT adapter, prompt FROM jobs").fetchone()
    assert row["adapter"] == "openai_compatible_images" and "Avoid" not in row["prompt"]


@respx.mock
def test_gemini_assets_are_marked_and_provenanced(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + ROWS.format(base="http://127.0.0.1:8100/v1"))
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
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
                ]
            },
        )
    )
    adapter = GeminiImageAdapter("gemini", "gemini-3.1-flash-image")
    ctx = _run("gemini", {"gemini": adapter}, studio_root, make_context, write_brief)
    _check_every_file(studio_root, "gemini_api", facade=False)
    # A wire with no negative-prompt field folds it: exactly what was sent.
    sent = json.loads(respx.calls.last.request.content)
    text = sent["contents"][0]["parts"][0]["text"]
    assert text.endswith("Avoid: text, logos")
    assert ctx.conn.execute("SELECT prompt FROM jobs").fetchone()["prompt"] == text


@respx.mock
def test_openai_assets_are_marked_and_provenanced(
    studio_root: Path,
    make_context: Callable[..., Context],
    write_brief: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + ROWS.format(base="http://127.0.0.1:8100/v1"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]}
        )
    )
    adapter = OpenAIImageAdapter("openai", "gpt-image-1")
    _run("openai", {"openai": adapter}, studio_root, make_context, write_brief)
    _check_every_file(studio_root, "openai_images", facade=False)


def test_fake_assets_are_marked_and_provenanced(
    studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    from qontinui_studio.adapters.fake import FakeImageAdapter

    _run("fake", {"fake": FakeImageAdapter("fake")}, studio_root, make_context, write_brief)
    _check_every_file(studio_root, "fake", facade=False)


def test_published_manifest_carries_each_files_provenance(
    studio_root: Path, make_context: Callable[..., Context], write_brief: Callable[..., Path]
) -> None:
    """A published file is a byte-identical copy of its marked variant; its
    sidecar travels in the site manifest, per format."""
    ctx = make_context()
    out = drive_batch(ctx, new_batch(ctx, load_brief(write_brief())))
    assert out.status == "published", out
    publish_dir = studio_root / "published" / "cafe"
    manifest = json.loads((publish_dir / "studio-manifest.json").read_text())
    entry = manifest["slots"]["hero"]
    assert entry["digital_source_type"] == marker.DIGITAL_SOURCE_TYPE
    for fmt, name in entry["files"].items():
        published = publish_dir / name
        assert marker.is_marked(published), published
        record = entry["provenance"][fmt]
        assert provenance.missing(record) == []
        assert record["file"]["sha256"] == entry["sha256"][fmt] == sha256_file(published)


def test_the_marker_preserves_pixels_and_is_idempotent() -> None:
    for fmt in ("PNG", "JPEG"):
        buf = io.BytesIO()
        Image.new("RGB", (16, 8), (10, 20, 30)).save(buf, fmt)
        data = buf.getvalue()
        marked = marker.mark_bytes(data)
        assert not marker.bytes_are_marked(data) and marker.bytes_are_marked(marked)
        assert marker.mark_bytes(marked) == marked
        with Image.open(io.BytesIO(data)) as a, Image.open(io.BytesIO(marked)) as b:
            assert a.format == b.format and a.tobytes() == b.tobytes()
    # WebP is transcoded losslessly to PNG: same decoded pixels.
    buf = io.BytesIO()
    Image.new("RGB", (16, 8), (10, 20, 30)).save(buf, "WEBP", lossless=True)
    marked = marker.mark_bytes(buf.getvalue())
    with Image.open(io.BytesIO(buf.getvalue())) as a, Image.open(io.BytesIO(marked)) as b:
        assert b.format == "PNG" and a.convert("RGB").tobytes() == b.convert("RGB").tobytes()


def test_an_incomplete_sidecar_is_never_written(tmp_path: Path) -> None:
    f = tmp_path / "x.png"
    f.write_bytes(_png(marked=True))
    record = provenance.build(
        file_sha256=sha256_file(f),
        file_name=f.name,
        role="raw",
        provider_id="local-gpu",
        provider_name="Local",
        adapter=provenance.LOCAL_ADAPTER,
        model="z",
        terms={"commercial_use": "yes"},
        prompt="p",
        negative_prompt=None,
        references=[],
        seed=None,
        recipe=None,
        created_at="2026-10-15T00:00:00+00:00",
        job_id="j",
        batch_id="b",
        brief_id="a",
        facade=None,  # the local provider without the facade's record
    )
    with pytest.raises(provenance.IncompleteProvenance, match="qontinui_provenance"):
        provenance.write(f, record)
    assert not provenance.sidecar_path(f).exists()


def test_the_shipped_inventory_has_a_local_gpu_row_on_loopback() -> None:
    inv = load_providers(Path(__file__).resolve().parent.parent / "providers.yaml")
    local = inv.get("local-gpu")
    assert local.adapter == "openai_compatible_images" and local.capacity == "unmetered"
    assert local.base_url == "http://127.0.0.1:8100/v1"
    assert local.daily_quota is None and local.schedulable_reason() is None
    # Cloud adapters stay under the launch kit's own quota: none recorded (D5).
    assert inv.get("gemini").daily_quota is None and inv.get("openai").daily_quota is None
