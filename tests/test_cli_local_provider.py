"""The provider interface observed where its users enter it: the ``studio`` CLI
and its scheduler (plan 2026-10-13 Phase 11, Arming). Nothing here imports the
code under test beyond the CLI itself; the files the CLI writes are read with
Pillow and ``json`` only.

* every file ``studio brief run`` writes carries the IPTC AI-generated marker
  and a provenance sidecar;
* a transport drop after the request was sent is reported ``unknown``, never
  ``failed``, and the job is retried;
* a refused connection is a retryable ``busy``: the job provably never started.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PIL import Image

from qontinui_studio import pipeline
from qontinui_studio.cli import main

from . import imagehost
from .conftest import FakeClock
from .imagehost import ImageHost

pytestmark = pytest.mark.local_http

TRAINED = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"

LOCAL_ROW = """
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


@pytest.fixture
def host() -> Iterator[ImageHost]:
    h = imagehost.start(ImageHost())
    yield h
    imagehost.stop(h)


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(pipeline, "utc_now", clock)
    monkeypatch.setattr(pipeline.time, "sleep", clock.sleep)
    return clock


def _studio(studio_root: Path, base: str, write_brief: Callable[..., Path]) -> list[str]:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text() + LOCAL_ROW.format(base=base))
    brief = write_brief(providers="local-gpu")
    return ["--root", str(studio_root), "brief", "run", "--no-publish", str(brief)]


def _exit(
    done: pytest.ExceptionInfo[SystemExit], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    """(exit code, output) of one ``studio`` run that ended in ``sys.exit``."""
    out = capsys.readouterr()
    code = done.value.code
    return (code if isinstance(code, int) else 0 if code is None else 1), out.out + out.err


def _xmp(path: Path) -> str:
    with Image.open(path) as img:
        raw = img.info.get("xmp") or img.info.get("XML:com.adobe.xmp") or b""
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def _ledger(studio_root: Path) -> list[tuple[str, str]]:
    db = sqlite3.Connection(studio_root / ".studio" / "studio.db")
    try:
        return [
            tuple(r) for r in db.execute("SELECT provider, outcome FROM job_attempts ORDER BY id")
        ]
    finally:
        db.close()


# The test signatures stay on one line (``fmt: skip``): these are the Phase 11
# arming tests, and the arming prover's Python extractor ends a definition at
# the close of a multi-line signature, so it would read none of the body.


def test_every_file_brief_run_writes_is_marked_and_has_a_provenance_sidecar(host: ImageHost, studio_root: Path, write_brief: Callable[..., Path], fake_time: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    # The host sends UNMARKED bytes: the studio's own writer must mark them.
    host.queue(
        imagehost.sse_reply(
            *imagehost.progress(),
            imagehost.completed(b64_json=imagehost.png_b64(marked=False)),
        )
    )
    # ``main`` in standalone mode, exactly as the ``studio`` console script runs it.
    with pytest.raises(SystemExit) as done:
        main(_studio(studio_root, host.v1_url, write_brief))
    code, output = _exit(done, capsys)
    assert code == 0, output
    written = sorted(
        p
        for d in ("raw", "variants")
        for p in (studio_root / ".studio" / d).rglob("*")
        if p.suffix in (".png", ".webp", ".avif")
    )
    assert len(written) == 6, written  # one raw + five derivatives
    for f in written:
        assert TRAINED in _xmp(f), f"{f.name} lacks the IPTC AI-generated marker"
        beside = f.with_name(f.name + ".provenance.json")
        assert beside.is_file(), f"{f.name} has no provenance sidecar"
        record = json.loads(beside.read_text())
        assert record["ai_generated"] is True and record["provider"]["id"] == "local-gpu"
        assert record["qontinui_provenance"] == imagehost.facade_provenance()


def test_a_transport_drop_after_the_request_was_sent_is_unknown_and_retried(host: ImageHost, studio_root: Path, write_brief: Callable[..., Path], fake_time: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    host.queue(
        imagehost.sse_reply(*imagehost.progress(), drop_after=2),
        imagehost.sse_reply(imagehost.completed()),
    )
    # ``main`` in standalone mode, exactly as the ``studio`` console script runs it.
    with pytest.raises(SystemExit) as done:
        main(_studio(studio_root, host.v1_url, write_brief))
    code, output = _exit(done, capsys)
    assert code == 0, output
    assert len(host.received) == 2, "the dropped job was retried"
    assert _ledger(studio_root) == [("local-gpu", "unknown"), ("local-gpu", "done")]


def test_a_refused_connection_is_a_retryable_busy(studio_root: Path, write_brief: Callable[..., Path], fake_time: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    base = f"http://127.0.0.1:{imagehost.closed_port()}/v1"
    with pytest.raises(SystemExit) as done:
        main(_studio(studio_root, base, write_brief))
    code, output = _exit(done, capsys)
    assert code == 3, output  # waiting, not failed
    assert "last attempt: busy from local-gpu" in output
    assert _ledger(studio_root) == [("local-gpu", "busy")]
    db = sqlite3.Connection(studio_root / ".studio" / "studio.db")
    status, tries = db.execute("SELECT status, attempts FROM jobs").fetchone()
    db.close()
    assert (status, tries) == ("queued", 0)
