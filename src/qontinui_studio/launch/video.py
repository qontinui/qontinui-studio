"""The launch film (~60 s, ffmpeg): title, the real walkthrough, build-record
summary slides, the build-record replay (a pluggable slot), optional AI b-roll
(labelled AI-generated inside the frame, D6) and a closing card.

The replay segment belongs to the sibling plan
(``2026-10-09-qontinui-marketing-studio-animated-process-explainers`` Phase 3).
Until a renderer for it exists the slot is empty and the film's manifest says
the segment was omitted and why — it is never faked with something else.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from qontinui_studio.errors import FfmpegFailed
from qontinui_studio.launch.record import BuildRecord
from qontinui_studio.postprocess import ffmpeg_exe, ffmpeg_path, run_ffmpeg

WIDTH = 1280
HEIGHT = 720
FPS = 30

REPLAY_OMITTED = (
    "omitted: the build-record replay scene (sibling plan "
    "2026-10-09-qontinui-marketing-studio-animated-process-explainers, Phase 3) "
    "has no renderer available to this kit"
)

#: Share of the film each segment gets. A segment that is absent gives its
#: share to the walkthrough, the real product being the point of the film.
SHARES = {
    "title": 0.05,
    "walkthrough": 0.45,
    "summary": 0.25,
    "replay": 0.10,
    "broll": 0.10,
    "closing": 0.05,
}


class ReplaySegment(Protocol):
    """The pluggable slot for the animated build-record replay."""

    name: str

    def render(
        self, record: BuildRecord, dest: Path, width: int, height: int, seconds: float
    ) -> Path:
        """Render a clip of about ``seconds`` to ``dest`` (any container ffmpeg reads)."""
        ...


@dataclass
class FileReplaySegment:
    """A replay clip rendered elsewhere (e.g. by the sibling plan's renderer)."""

    path: Path
    name: str = "replay-file"

    def render(
        self, record: BuildRecord, dest: Path, width: int, height: int, seconds: float
    ) -> Path:
        return self.path


@dataclass
class SegmentRecord:
    name: str
    seconds: float
    source: str
    note: str | None = None


@dataclass
class FilmPlan:
    """What went into the film — written beside it as ``film.json``."""

    target_seconds: float
    segments: list[SegmentRecord] = field(default_factory=list)
    omitted: dict[str, str] = field(default_factory=dict)
    output: str | None = None
    actual_seconds: float | None = None


@dataclass(frozen=True)
class Still:
    path: Path
    seconds: float


def _encode_args() -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]


def _scale_filter(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x12141c,setsar=1,fps={FPS}"
    )


def stills_clip(stills: list[Still], dest: Path, width: int = WIDTH, height: int = HEIGHT) -> Path:
    """Stitch still frames into one clip, each held for its ``seconds`` — the
    walkthrough path when video recording is unavailable."""
    if not stills:
        raise ValueError("stills_clip needs at least one frame")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    for still in stills:
        args += ["-loop", "1", "-t", f"{still.seconds:.3f}", "-i", ffmpeg_path(still.path)]
    chains = [f"[{i}:v]{_scale_filter(width, height)}[v{i}]" for i in range(len(stills))]
    joined = "".join(f"[v{i}]" for i in range(len(stills)))
    graph = ";".join(chains) + f";{joined}concat=n={len(stills)}:v=1:a=0[out]"
    run_ffmpeg(
        [*args, "-filter_complex", graph, "-map", "[out]", *_encode_args(), ffmpeg_path(dest)]
    )
    return dest


def video_clip(
    src: Path, dest: Path, max_seconds: float, width: int = WIDTH, height: int = HEIGHT
) -> Path:
    """Normalize a recorded clip (Playwright webm) to the film's size/rate, at most ``max_seconds``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-i",
            ffmpeg_path(src),
            "-t",
            f"{max_seconds:.3f}",
            "-vf",
            _scale_filter(width, height),
            *_encode_args(),
            ffmpeg_path(dest),
        ]
    )
    return dest


def concat_quote(path: Path) -> str:
    """A path quoted for ffmpeg's concat-demuxer list: single-quoted, each
    ``'`` written as ``'\\''`` (close, escaped quote, reopen) — so a path
    like ``o'brien/a.mp4`` is read as itself."""
    return "'" + str(path).replace("'", "'\\''") + "'"


def concat(clips: list[Path], dest: Path) -> Path:
    """Join clips encoded with identical parameters (every clip here is)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    listing = dest.with_suffix(".concat.txt")
    listing.write_text(
        "".join(f"file {concat_quote(c.resolve())}\n" for c in clips), encoding="utf-8"
    )
    try:
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                ffmpeg_path(listing),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                ffmpeg_path(dest),
            ]
        )
    finally:
        listing.unlink(missing_ok=True)
    return dest


def media_duration(path: Path) -> float:
    """Duration in seconds (ffprobe when present, else ffmpeg's banner)."""
    probe = shutil.which("ffprobe")
    if probe:
        proc = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                ffmpeg_path(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            return float(proc.stdout.strip())
        except ValueError:
            pass
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", ffmpeg_path(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        raise FfmpegFailed(f"cannot read the duration of {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


@dataclass
class FilmInputs:
    title_frame: Path
    closing_frame: Path
    summary_frames: list[Path]
    walkthrough_video: Path | None = None
    walkthrough_stills: list[Still] = field(default_factory=list)
    broll_frames: list[Path] = field(default_factory=list)
    replay: ReplaySegment | None = None


def compose_film(
    inputs: FilmInputs, record: BuildRecord, out_dir: Path, target_seconds: float = 60.0
) -> FilmPlan:
    """Render ``<out_dir>/launch.mp4`` and ``<out_dir>/film.json``.

    Segments are rendered in a temporary directory OUTSIDE ``out_dir`` and
    removed whatever happens, and the film lands in ``out_dir`` only once it
    is complete — a failed render leaves nothing behind in the bundle."""
    work = Path(tempfile.mkdtemp(prefix="studio-film-"))
    try:
        return _compose_film(inputs, record, out_dir, target_seconds, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _compose_film(
    inputs: FilmInputs, record: BuildRecord, out_dir: Path, target_seconds: float, work: Path
) -> FilmPlan:
    plan = FilmPlan(target_seconds=target_seconds)
    has_walk = inputs.walkthrough_video is not None or bool(inputs.walkthrough_stills)
    if inputs.replay is None:
        plan.omitted["replay"] = REPLAY_OMITTED
    if not inputs.broll_frames:
        plan.omitted["broll"] = "no published decorative asset (AI b-roll is optional)"
    if not has_walk:
        plan.omitted["walkthrough"] = "no capture of the running product is available"
    present = {
        "title": True,
        "walkthrough": has_walk,
        "summary": bool(inputs.summary_frames),
        "replay": inputs.replay is not None,
        "broll": bool(inputs.broll_frames),
        "closing": True,
    }
    spare = sum(share for name, share in SHARES.items() if not present[name])
    budget = {name: SHARES[name] * target_seconds for name in SHARES if present[name]}
    receiver = "walkthrough" if has_walk else "summary" if present["summary"] else "title"
    budget[receiver] = budget.get(receiver, 0.0) + spare * target_seconds

    clips: list[Path] = []

    def still_segment(
        name: str, frames: list[Path], seconds: float, source: str, note: str | None = None
    ) -> None:
        each = seconds / len(frames)
        dest = stills_clip([Still(f, each) for f in frames], work / f"{len(clips):02d}-{name}.mp4")
        clips.append(dest)
        plan.segments.append(SegmentRecord(name, round(seconds, 3), source, note))

    still_segment("title", [inputs.title_frame], budget["title"], "product definition")
    if has_walk:
        seconds = budget["walkthrough"]
        stills = inputs.walkthrough_stills
        if inputs.walkthrough_video is not None:
            dest = video_clip(
                inputs.walkthrough_video, work / f"{len(clips):02d}-walkthrough.mp4", seconds
            )
            clips.append(dest)
            recorded = media_duration(dest)
            plan.segments.append(
                SegmentRecord(
                    "walkthrough",
                    round(recorded, 3),
                    "Playwright recording of the running product",
                )
            )
            # A quick script records a short clip: the screenshots of the same
            # run hold the rest of the walkthrough's share.
            rest = seconds - recorded
            if rest >= 0.5 and stills:
                still_segment(
                    "walkthrough-stills",
                    [s.path for s in stills],
                    rest,
                    "screenshots of the running product stitched by ffmpeg",
                    "holds the walkthrough's remaining share after the recording",
                )
        else:
            weight = sum(s.seconds for s in stills)
            scaled = [Still(s.path, seconds * s.seconds / weight) for s in stills]
            dest = stills_clip(scaled, work / f"{len(clips):02d}-walkthrough.mp4")
            clips.append(dest)
            plan.segments.append(
                SegmentRecord(
                    "walkthrough",
                    round(seconds, 3),
                    "screenshots of the running product stitched by ffmpeg",
                    "video recording unavailable; stills used",
                )
            )
    if present["summary"]:
        still_segment(
            "summary",
            inputs.summary_frames,
            budget["summary"],
            "build record (every value names its field)",
        )
    if inputs.replay is not None:
        raw = inputs.replay.render(record, work / "replay-raw.mp4", WIDTH, HEIGHT, budget["replay"])
        dest = video_clip(raw, work / f"{len(clips):02d}-replay.mp4", budget["replay"])
        clips.append(dest)
        plan.segments.append(
            SegmentRecord(
                "replay", round(media_duration(dest), 3), f"replay segment '{inputs.replay.name}'"
            )
        )
    if present["broll"]:
        still_segment(
            "broll",
            inputs.broll_frames,
            budget["broll"],
            "asset library (AI-generated, labelled in frame)",
        )
    still_segment(
        "closing",
        [inputs.closing_frame],
        budget["closing"],
        "product definition + public build-record URL",
    )

    rendered = work / "launch.mp4"
    concat(clips, rendered)
    out_dir.mkdir(parents=True, exist_ok=True)
    film = out_dir / "launch.mp4"
    shutil.move(str(rendered), film)
    plan.output = film.name
    plan.actual_seconds = round(media_duration(film), 3)
    (out_dir / "film.json").write_text(json.dumps(asdict(plan), indent=2), encoding="utf-8")
    return plan
