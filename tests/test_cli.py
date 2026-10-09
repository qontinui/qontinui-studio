from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from qontinui_studio import cli, pipeline
from qontinui_studio.adapters import AdapterSet
from qontinui_studio.adapters.fake import FakeImageAdapter
from qontinui_studio.cli import main

from .conftest import FakeClock, FakePreview

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fake_preview(monkeypatch: pytest.MonkeyPatch, preview: FakePreview) -> None:
    """The CLI's Context talks to the fake preview server instead of HTTP."""
    real = cli._context

    def with_preview(root: Path) -> pipeline.Context:
        ctx = real(root)
        ctx.bridge = preview.bridge
        ctx.fetch = preview.fetch
        return ctx

    monkeypatch.setattr(cli, "_context", with_preview)


@pytest.fixture(autouse=True)
def _no_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_COORD_BEARER", raising=False)
    monkeypatch.delenv("STUDIO_COORD_BEARER_FILE", raising=False)


def test_cli_run_list_review_unpublish(studio_root: Path, write_brief: Callable[..., Path]) -> None:
    runner = CliRunner()
    root = ["--root", str(studio_root)]
    brief = write_brief()

    r = runner.invoke(main, [*root, "brief", "validate", str(brief)])
    assert r.exit_code == 0, r.output

    r = runner.invoke(main, [*root, "brief", "run", str(brief)])
    assert r.exit_code == 0, r.output
    assert ": published" in r.output
    batch_id = r.output.split()[1].rstrip(":")
    assert (studio_root / "published" / "cafe" / "hero.webp").is_file()

    r = runner.invoke(main, [*root, "library", "list", "--json"])
    rows = json.loads(r.output)
    assert rows[0]["ai_generated"] is True and rows[0]["licence_tos_reviewed"] == "2026-10-09"

    r = runner.invoke(main, [*root, "review", "build"])
    assert r.exit_code == 0 and Path(r.output.strip()).is_file()

    r = runner.invoke(main, [*root, "unpublish", "--batch", batch_id])
    assert r.exit_code == 0, r.output
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()

    r = runner.invoke(main, [*root, "publish", "--batch", batch_id])
    assert r.exit_code == 0, r.output
    assert (studio_root / "published" / "cafe" / "hero.webp").is_file()


def test_cli_refusal_exit_code(studio_root: Path, write_brief: Callable[..., Path]) -> None:
    r = CliRunner().invoke(
        main, ["--root", str(studio_root), "brief", "run", str(write_brief(providers="fake2"))]
    )
    assert r.exit_code == 2, r.output
    assert "d5_licence fail" in r.output


def test_cli_providers_list_on_the_shipped_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    r = CliRunner().invoke(main, ["--root", str(REPO), "providers", "list"])
    assert r.exit_code == 0
    assert "bing" in r.output and "off: automation_allowed is 'no'" in r.output


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


@needs_ffmpeg
def test_cli_media_trim_loop_boomerang(tmp_path: Path) -> None:
    src = tmp_path / "-starts-with-dash.mp4"  # file: URLs keep this from being an option
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=10:duration=2",
            "-pix_fmt",
            "yuv420p",
            "file:" + str(src),
        ],
        check=True,
    )
    runner = CliRunner()
    trimmed = tmp_path / "t.mp4"
    r = runner.invoke(
        main, ["media", "trim", str(src), str(trimmed), "--start", "0.5", "--duration", "1"]
    )
    assert r.exit_code == 0, r.output
    assert abs(_duration(trimmed) - 1.0) < 0.25
    looped = tmp_path / "l.mp4"
    r = runner.invoke(main, ["media", "loop", str(src), str(looped), "--count", "3"])
    assert r.exit_code == 0, r.output
    assert abs(_duration(looped) - 6.0) < 0.5
    boom = tmp_path / "b.mp4"
    r = runner.invoke(main, ["media", "loop", str(src), str(boom), "--boomerang"])
    assert r.exit_code == 0, r.output
    assert abs(_duration(boom) - 4.0) < 0.5


def test_cli_brief_run_backs_off_through_transient_failures(
    studio_root: Path, write_brief: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    flaky = FakeImageAdapter("fake", "fake-v1", fail_first=2)
    monkeypatch.setattr(cli, "build_adapters", lambda inv: AdapterSet(adapters={"fake": flaky}))
    # Fake time advances with each sleep so the backoff is observable.
    clock = FakeClock()

    def sleep(s: float) -> None:
        slept.append(s)
        clock.sleep(s)

    monkeypatch.setattr(pipeline.time, "sleep", sleep)
    monkeypatch.setattr(pipeline, "utc_now", clock)
    r = CliRunner().invoke(main, ["--root", str(studio_root), "brief", "run", str(write_brief())])
    assert r.exit_code == 0, r.output
    assert slept[:2] == [30.0, 60.0]  # exponential backoff between the three attempts


def test_cli_publish_unknown_batch_is_a_clean_error(studio_root: Path) -> None:
    r = CliRunner().invoke(main, ["--root", str(studio_root), "publish", "--batch", "nope"])
    assert r.exit_code == 1 and "no batch 'nope'" in r.output


def test_cli_jobs_run_resumes_a_quota_deferred_batch(
    studio_root: Path, write_brief: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = studio_root / "providers.yaml"
    p.write_text(p.read_text().replace("daily_quota: 10", "daily_quota: 1", 1))
    clock = FakeClock()
    monkeypatch.setattr(pipeline, "utc_now", clock)
    monkeypatch.setattr(pipeline.time, "sleep", clock.sleep)
    runner = CliRunner()
    root = ["--root", str(studio_root)]
    r = runner.invoke(main, [*root, "brief", "run", str(write_brief(variants=2))])
    assert r.exit_code == 3, r.output
    assert "deferred" in r.output
    assert not (studio_root / "published" / "cafe" / "hero.webp").exists()

    r = runner.invoke(main, [*root, "jobs", "run"])
    assert r.exit_code == 3 and "generating" in r.output  # same day: still deferred

    clock.sleep(24 * 3600)
    r = runner.invoke(main, [*root, "jobs", "run"])
    assert r.exit_code == 0, r.output
    assert ": published" in r.output
    assert (studio_root / "published" / "cafe" / "hero.webp").is_file()
