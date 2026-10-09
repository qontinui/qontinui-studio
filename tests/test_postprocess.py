from __future__ import annotations

import builtins
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from qontinui_studio import postprocess
from qontinui_studio.errors import (
    BackgroundRemovalUnavailable,
    FfmpegFailed,
    FfmpegUnavailable,
    FormatUnsupported,
)


def test_crop_to_slot_is_exact_and_upscales_small_sources() -> None:
    small = Image.new("RGB", (320, 180), "red")
    out = postprocess.crop_to_slot(small, 1440, 1080)
    assert out.size == (1440, 1080)
    wide = Image.new("RGB", (3000, 1000), "blue")
    assert postprocess.crop_to_slot(wide, 600, 600, (0.0, 0.5)).size == (600, 600)


def test_resize_and_upscale() -> None:
    img = Image.new("RGB", (100, 50))
    assert postprocess.resize(img, 40, 40).size == (40, 40)
    assert postprocess.upscale(img, 2.5).size == (250, 125)
    with pytest.raises(ValueError):
        postprocess.upscale(img, 0)


@pytest.mark.parametrize("fmt", ["webp", "avif", "png", "jpeg"])
def test_export_formats(tmp_path: Path, fmt: str) -> None:
    img = Image.new("RGBA", (64, 48), (1, 2, 3, 255))
    out = postprocess.export(img, tmp_path / "x", fmt)
    with Image.open(out) as back:
        assert back.size == (64, 48)


def test_export_avif_unsupported_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postprocess.features, "check", lambda name: False)
    with pytest.raises(FormatUnsupported):
        postprocess.export(Image.new("RGB", (4, 4)), tmp_path / "x", "avif")


def test_background_removal_without_rembg_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_rembg(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "rembg":
            raise ImportError("no rembg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_rembg)
    with pytest.raises(BackgroundRemovalUnavailable, match="poetry install -E bg"):
        postprocess.remove_background(Image.new("RGB", (4, 4)))


def test_ffmpeg_missing_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postprocess.shutil, "which", lambda name: None)
    with pytest.raises(FfmpegUnavailable):
        postprocess.trim(tmp_path / "a.mp4", tmp_path / "b.mp4", 0, 1)


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    src = tmp_path / "src.mp4"
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
            str(src),
        ],
        check=True,
    )
    return src


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


@needs_ffmpeg
def test_ffmpeg_trim_loop_boomerang(clip: Path, tmp_path: Path) -> None:
    trimmed = postprocess.trim(clip, tmp_path / "t.mp4", 0.5, 1.0)
    assert abs(_duration(trimmed) - 1.0) < 0.25
    looped = postprocess.loop(clip, tmp_path / "l.mp4", 3)
    assert abs(_duration(looped) - 6.0) < 0.5
    boom = postprocess.boomerang(clip, tmp_path / "b.mp4")
    assert abs(_duration(boom) - 4.0) < 0.5


@needs_ffmpeg
def test_ffmpeg_failure_is_typed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(FfmpegFailed):
        postprocess.trim(bad, tmp_path / "o.mp4", 0, 1)
