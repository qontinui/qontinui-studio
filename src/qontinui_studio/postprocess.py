"""Local, free post-processing: crop-to-slot, resize/upscale, WebP/AVIF export,
SVG tracing (vtracer), background removal (optional ``rembg``), and ffmpeg
trims/loops.

Every missing tool is a typed error, never a silent skip.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps, features

from qontinui_studio import marker
from qontinui_studio.errors import (
    BackgroundRemovalUnavailable,
    FfmpegFailed,
    FfmpegUnavailable,
    FormatUnsupported,
)

RESAMPLE = Image.Resampling.LANCZOS


def crop_to_slot(
    img: Image.Image, width: int, height: int, focal: tuple[float, float] = (0.5, 0.5)
) -> Image.Image:
    """Scale to cover ``width``x``height`` (upscaling with Lanczos when the source
    is smaller), then crop around the focal point to exactly that size."""
    return ImageOps.fit(img, (width, height), method=RESAMPLE, centering=focal)


def resize(img: Image.Image, width: int, height: int) -> Image.Image:
    return img.resize((width, height), RESAMPLE)


def upscale(img: Image.Image, factor: float) -> Image.Image:
    if factor <= 0:
        raise ValueError("upscale factor must be positive")
    return img.resize((round(img.width * factor), round(img.height * factor)), RESAMPLE)


def remove_background(img: Image.Image) -> Image.Image:
    try:
        from rembg import remove
    except ImportError as e:
        raise BackgroundRemovalUnavailable(
            "background removal needs the optional 'rembg' dependency: poetry install -E bg"
        ) from e
    out = remove(img)
    if not isinstance(out, Image.Image):
        raise BackgroundRemovalUnavailable("rembg returned a non-image result")
    return out


def rembg_available() -> bool:
    try:
        import rembg  # noqa: F401
    except ImportError:
        return False
    return True


def format_supported(fmt: str) -> bool:
    if fmt == "svg":
        return vtracer_available()
    if fmt not in _PIL_FORMAT:
        return False
    if fmt in ("avif", "webp"):
        return bool(features.check(fmt))
    return True


_PIL_FORMAT = {"webp": "WEBP", "avif": "AVIF", "png": "PNG", "jpeg": "JPEG"}
_EXT = {"webp": ".webp", "avif": ".avif", "png": ".png", "jpeg": ".jpg", "svg": ".svg"}

# vtracer's parameters, written out as the image licence manifest's
# ``empty-state`` recipe writes them (vtracer 0.6.15's documented defaults), so a
# changed default in a later vtracer cannot change a trace unseen.
TRACE_PARAMETERS: dict[str, object] = {
    "colormode": "color",
    "hierarchical": "stacked",
    "mode": "spline",
    "filter_speckle": 4,
    "color_precision": 6,
    "layer_difference": 16,
    "corner_threshold": 60,
    "length_threshold": 4.0,
    "max_iterations": 10,
    "splice_threshold": 45,
    "path_precision": 8,
}


def vtracer_available() -> bool:
    try:
        import vtracer  # noqa: F401
    except ImportError:
        return False
    return True


def trace_svg(img: Image.Image) -> str:
    """Trace ``img`` to an SVG document with vtracer. Fully transparent pixels
    are keyed out, so a matted subject carries no background into either
    theme. The root gets a ``viewBox`` (so the trace scales to any box) beside
    the pixel ``width``/``height`` vtracer writes (the intrinsic size a browser
    reports as ``naturalWidth``)."""
    try:
        import vtracer
    except ImportError as e:
        raise FormatUnsupported("SVG tracing needs vtracer (a studio dependency)") from e
    buf = io.BytesIO()
    img.save(buf, "PNG")
    text = vtracer.convert_raw_image_to_svg(buf.getvalue(), img_format="png", **TRACE_PARAMETERS)
    root = marker.svg_root(text.encode("utf-8"))
    if root is None:
        raise FormatUnsupported("vtracer did not return an SVG document")
    root.set("viewBox", f"0 0 {img.width} {img.height}")
    root.set("width", str(img.width))
    root.set("height", str(img.height))
    return marker.serialise_svg(root)


def image_size(path: Path) -> tuple[int, int]:
    """(width, height) in pixels: Pillow's for a raster, the root's
    ``width``/``height`` for an SVG trace."""
    if path.suffix == ".svg":
        root = marker.svg_root(path.read_bytes())
        if root is None:
            raise ValueError(f"{path} is not an SVG document")
        return int(float(root.get("width", "0"))), int(float(root.get("height", "0")))
    with Image.open(path) as img:
        return img.size


def export(
    img: Image.Image, dest_stem: Path, fmt: str, quality: int = 82, *, ai_generated: bool
) -> Path:
    """Write ``img`` as ``fmt`` next to ``dest_stem`` and return the path. An
    ``ai_generated`` image carries the IPTC AI-generated marker (``marker``)."""
    if not format_supported(fmt):
        raise FormatUnsupported(f"this Pillow build cannot encode '{fmt}'")
    dest = dest_stem.with_suffix(_EXT[fmt])
    dest.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "svg":
        svg = trace_svg(img)
        dest.write_text(marker.mark_svg(svg) if ai_generated else svg, encoding="utf-8")
        return dest
    out = img
    if fmt == "jpeg" and out.mode not in ("RGB", "L"):
        out = out.convert("RGB")
    kwargs: dict[str, object] = dict(marker.save_options(fmt)) if ai_generated else {}
    if fmt in ("webp", "avif", "jpeg"):
        kwargs["quality"] = quality
    out.save(dest, _PIL_FORMAT[fmt], **kwargs)
    return dest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- ffmpeg -----------------------------------------------------------------


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise FfmpegUnavailable("ffmpeg is not on PATH (apt install ffmpeg)")
    return exe


def run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise FfmpegFailed(f"ffmpeg exited {proc.returncode}: {proc.stderr.strip()[-500:]}")


def ffmpeg_path(path: Path) -> str:
    """An ffmpeg URL for a local file: ``file:`` + the resolved path, so a name
    starting with ``-`` or containing ``:`` is never read as an option or protocol."""
    return "file:" + str(path.resolve())


def trim(src: Path, dest: Path, start_s: float, duration_s: float) -> Path:
    """Cut ``duration_s`` seconds from ``start_s`` (re-encoded, so cuts are exact)."""
    if duration_s <= 0 or start_s < 0:
        raise ValueError("trim needs start >= 0 and duration > 0")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-ss",
            f"{start_s}",
            "-i",
            ffmpeg_path(src),
            "-t",
            f"{duration_s}",
            "-an",
            ffmpeg_path(dest),
        ]
    )
    return dest


def loop(src: Path, dest: Path, count: int) -> Path:
    """Play ``src`` ``count`` times back to back."""
    if count < 1:
        raise ValueError("loop count must be >= 1")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-stream_loop", str(count - 1), "-i", ffmpeg_path(src), "-an", ffmpeg_path(dest)])
    return dest


def boomerang(src: Path, dest: Path) -> Path:
    """A seamless loop: the clip forwards then reversed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-i",
            ffmpeg_path(src),
            "-filter_complex",
            "[0:v]reverse[r];[0:v][r]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            ffmpeg_path(dest),
        ]
    )
    return dest
