/**
 * The unattended publish gate's checks (plan Phase 5). Each is a pure
 * judgement of data already read; ./gate.ts reads it. A check that could
 * not be measured FAILS (UNKNOWN never passes). The four media checks —
 * ffprobe sanity, black frames, audio silence and clipping, captions — are
 * this studio's own implementation of checks named in calesthio/OpenMontage
 * (AGPL); no code was taken from it.
 */
import type { NarrationTiming } from "../api/narration-timing";
import type { CompositionManifest } from "./manifest";
import type { AudioStats, FrameStats, Probe } from "./probe";

export type CheckStatus = "pass" | "fail";

export interface CheckResult {
  name: string;
  status: CheckStatus;
  /** One line a reader can act on. */
  detail: string;
  /** Problems that do not fail the check, shown on the review page. */
  warnings?: string[];
  evidence?: unknown;
}

export interface GateThresholds {
  /** Speech match each narrated segment must reach (0..1). */
  minSpeechMatch: number;
  /** A silence at least this long (s) fails the audio check. */
  maxSilenceSeconds: number;
  /** Peak level (dBFS) at or above which audio counts as clipped. */
  clipDb: number;
  /** Mean level (dBFS) below which the whole track counts as silent. */
  silentMeanDb: number;
  /** A frame whose 99.9th-percentile luma is at or below this is black (nothing in it is lit). */
  blackPeak: number;
  /** A frame whose luma standard deviation is below this is a flat field. */
  flatStddev: number;
}

export const DEFAULT_THRESHOLDS: GateThresholds = {
  minSpeechMatch: 0.9,
  maxSilenceSeconds: 10,
  clipDb: -0.1,
  silentMeanDb: -60,
  blackPeak: 40,
  flatStddev: 1,
};

const pass = (name: string, detail: string, extra: Partial<CheckResult> = {}): CheckResult => ({ name, status: "pass", detail, ...extra });
const fail = (name: string, detail: string, extra: Partial<CheckResult> = {}): CheckResult => ({ name, status: "fail", detail, ...extra });

/** The four points sampled for black frames: the middles of the four quarters. */
export function samplePoints(durationSeconds: number): number[] {
  return [0.125, 0.375, 0.625, 0.875].map((f) => Math.round(durationSeconds * f * 1000) / 1000);
}

function rate(text: string | undefined): number | null {
  if (!text) return null;
  const [n, d] = text.split("/").map(Number);
  if (n === undefined || !Number.isFinite(n) || n <= 0) return null;
  if (d === undefined) return n;
  return d > 0 ? n / d : null;
}

const VIDEO_CODEC = { mp4: "h264", webm: "vp8" } as const;
const AUDIO_CODECS = { mp4: ["aac"], webm: ["opus", "vorbis"] } as const;

export function checkProbe(manifest: CompositionManifest, probe: Probe): CheckResult {
  const name = "ffprobe";
  const problems: string[] = [];
  const streams = probe.streams ?? [];
  const video = streams.filter((s) => s.codec_type === "video");
  const audio = streams.filter((s) => s.codec_type === "audio");
  const duration = Number(probe.format?.duration);
  const expected = manifest.duration_in_frames / manifest.fps;
  if (!Number.isFinite(duration) || duration <= 0) problems.push("no duration");
  // Two frames of slack: AAC priming can make the container a frame long.
  else if (Math.abs(duration - expected) > 2 / manifest.fps) problems.push(`duration ${duration.toFixed(3)} s, expected ${expected.toFixed(3)} s`);
  if (video.length !== 1) problems.push(`${video.length} video streams, expected 1`);
  const v = video[0];
  if (v) {
    if (v.codec_name !== VIDEO_CODEC[manifest.format]) problems.push(`video codec ${v.codec_name}, expected ${VIDEO_CODEC[manifest.format]}`);
    if (v.width !== manifest.width || v.height !== manifest.height) problems.push(`size ${v.width}x${v.height}, expected ${manifest.width}x${manifest.height}`);
    const fps = rate(v.avg_frame_rate) ?? rate(v.r_frame_rate);
    if (fps === null || Math.abs(fps - manifest.fps) > 0.01) problems.push(`frame rate ${v.avg_frame_rate ?? v.r_frame_rate}, expected ${manifest.fps}`);
    // 8-bit 4:2:0 (Remotion's H.264 is flagged full-range, yuvj420p): what every player and network takes.
    // The container's duration is its longest stream: the video stream must itself be full length.
    const frames = Number(v.nb_read_packets ?? v.nb_frames);
    const vDuration = Number(v.duration);
    if (Number.isFinite(frames) && frames > 0) {
      if (Math.abs(frames - manifest.duration_in_frames) > 1) problems.push(`the video stream has ${frames} frames, expected ${manifest.duration_in_frames}`);
    } else if (Number.isFinite(vDuration) && vDuration > 0) {
      if (Math.abs(vDuration - expected) > 2 / manifest.fps) problems.push(`the video stream lasts ${vDuration.toFixed(3)} s, expected ${expected.toFixed(3)} s`);
    } else problems.push("the video stream's length could not be read");
    if (v.pix_fmt !== "yuv420p" && v.pix_fmt !== "yuvj420p") problems.push(`pixel format ${v.pix_fmt}, expected 8-bit 4:2:0 (yuv420p)`);
  }
  if (manifest.narration) {
    if (audio.length !== 1) problems.push(`${audio.length} audio streams, expected 1`);
    else if (!(AUDIO_CODECS[manifest.format] as readonly string[]).includes(audio[0]?.codec_name ?? "")) {
      problems.push(`audio codec ${audio[0]?.codec_name}, expected ${AUDIO_CODECS[manifest.format].join(" or ")}`);
    }
  }
  const evidence = { duration, video: v ?? null, audio: audio.map((a) => ({ codec: a.codec_name, sample_rate: a.sample_rate, channels: a.channels })) };
  return problems.length
    ? fail(name, problems.join("; "), { evidence })
    : pass(name, `${manifest.width}x${manifest.height} ${VIDEO_CODEC[manifest.format]} @ ${manifest.fps} fps, ${duration.toFixed(2)} s`, { evidence });
}

export function checkFrames(stats: readonly FrameStats[], t: GateThresholds = DEFAULT_THRESHOLDS): CheckResult {
  const name = "frames";
  if (stats.length !== 4) return fail(name, `sampled ${stats.length} frames, need 4`, { evidence: stats });
  const bad = stats.filter((s) => s.peak <= t.blackPeak || s.stddev < t.flatStddev);
  if (bad.length) {
    return fail(
      name,
      bad.map((s) => `${s.at.toFixed(2)} s is ${s.peak <= t.blackPeak ? "black" : "a flat field"} (peak luma ${s.peak}, sd ${s.stddev.toFixed(1)})`).join("; "),
      { evidence: stats },
    );
  }
  return pass(name, `4 frames sampled (${stats.map((s) => `${s.at.toFixed(1)} s`).join(", ")}), none black or flat`, { evidence: stats });
}

export function checkAudio(stats: AudioStats | null, durationSeconds: number, t: GateThresholds = DEFAULT_THRESHOLDS): CheckResult {
  const name = "audio";
  if (!stats) return fail(name, "no audio stream: a video publishes only with its narration");
  const problems: string[] = [];
  if (stats.meanVolume === null || stats.maxVolume === null) problems.push("the level could not be measured");
  else {
    if (stats.meanVolume < t.silentMeanDb) problems.push(`the track is silent (mean ${stats.meanVolume} dB)`);
    if (stats.maxVolume >= t.clipDb) problems.push(`clipping: peak ${stats.maxVolume} dB`);
  }
  for (const s of stats.silences) {
    const end = s.end ?? durationSeconds;
    if (end - s.start >= t.maxSilenceSeconds - 1e-6) problems.push(`${(end - s.start).toFixed(1)} s of silence from ${s.start.toFixed(1)} s`);
  }
  return problems.length
    ? fail(name, problems.join("; "), { evidence: stats })
    : pass(name, `mean ${stats.meanVolume} dB, peak ${stats.maxVolume} dB, no silence of ${t.maxSilenceSeconds} s or more`, { evidence: stats });
}

export interface VttCue {
  start: number;
  end: number;
  text: string;
}

function vttTime(text: string): number | null {
  const m = /^(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})$/.exec(text.trim());
  if (!m) return null;
  return Number(m[1] ?? 0) * 3600 + Number(m[2]) * 60 + Number(m[3]) + Number(m[4]) / 1000;
}

/** Parse WebVTT cues; throws on anything malformed. */
export function parseVtt(text: string): VttCue[] {
  const blocks = text.replace(/\r\n?/g, "\n").split(/\n{2,}/);
  if (!/^WEBVTT(\s|$)/.test(blocks[0] ?? "")) throw new Error("does not start with WEBVTT");
  const cues: VttCue[] = [];
  for (const block of blocks.slice(1)) {
    const lines = block.split("\n").filter((l) => l.length);
    if (!lines.length) continue;
    const timing = lines.findIndex((l) => l.includes("-->"));
    if (timing < 0) throw new Error(`a cue without timing: ${lines[0]}`);
    const [a, b] = (lines[timing] as string).split("-->");
    const start = vttTime(a ?? "");
    const end = vttTime((b ?? "").trim().split(/\s+/)[0] ?? "");
    if (start === null || end === null) throw new Error(`bad cue timing: ${lines[timing]}`);
    const body = lines.slice(timing + 1).join(" ").trim();
    if (!body) throw new Error(`an empty cue at ${a?.trim()}`);
    cues.push({ start, end, text: body });
  }
  return cues;
}

/** Where each narration clip plays, in frames. */
export interface ClipPlacement {
  segment: string;
  start_frame: number;
  duration_in_frames: number;
}

/**
 * `expected` is the captions file the rebuilt program and timing table
 * produce (null when they produce none): the file beside the video must be
 * exactly that, so a stale or edited file never passes.
 */
export function checkCaptions(
  manifest: CompositionManifest,
  clips: readonly ClipPlacement[],
  vtt: string | null,
  expected: string | null,
  durationSeconds: number,
): CheckResult {
  const name = "captions";
  if (!clips.length) return fail(name, "no narration, so no captions");
  if (!manifest.captions) return fail(name, "the manifest records no captions");
  if (vtt === null) return fail(name, `${manifest.captions.file} is missing`);
  if (expected === null || vtt !== expected) {
    return fail(name, `${manifest.captions.file} is not the captions this program and its timing table produce (stale or edited)`);
  }
  let cues: VttCue[];
  try {
    cues = parseVtt(vtt);
  } catch (err) {
    return fail(name, `${manifest.captions.file} is not valid WebVTT: ${err instanceof Error ? err.message : String(err)}`);
  }
  const problems: string[] = [];
  if (cues.length !== manifest.captions.cues) problems.push(`${cues.length} cues, the manifest says ${manifest.captions.cues}`);
  cues.forEach((c, i) => {
    if (c.end <= c.start) problems.push(`cue ${i + 1} ends before it starts`);
    if (c.end > durationSeconds + 1e-3) problems.push(`cue ${i + 1} runs past the video's end`);
    if (i && c.start < (cues[i - 1] as VttCue).start) problems.push(`cue ${i + 1} is out of order`);
  });
  for (const clip of clips) {
    const from = clip.start_frame / manifest.fps;
    const to = (clip.start_frame + clip.duration_in_frames) / manifest.fps;
    if (!cues.some((c) => c.end > from && c.start < to)) problems.push(`no caption while segment "${clip.segment}" is narrated`);
  }
  return problems.length
    ? fail(name, problems.join("; "))
    : pass(name, `${cues.length} cues in ${manifest.captions.file}, covering every narrated segment`);
}

/** `clips` are the rebuilt program's narration clips, never the manifest's own list. */
export function checkSpeech(
  clips: readonly ClipPlacement[],
  timing: NarrationTiming | null,
  t: GateThresholds = DEFAULT_THRESHOLDS,
): CheckResult {
  const name = "speech";
  if (!timing || !clips.length) return fail(name, "no narration: nothing was spoken, so nothing can be checked");
  const problems: string[] = [];
  const evidence: Record<string, unknown> = {};
  for (const clip of clips) {
    const speech = timing.segments[clip.segment]?.speech;
    evidence[clip.segment] = speech ?? null;
    if (!speech) problems.push(`${clip.segment}: never checked`);
    else if (speech.status === "unavailable") problems.push(`${clip.segment}: the speech check was unavailable (${speech.reason})`);
    else if (speech.match < t.minSpeechMatch) {
      problems.push(`${clip.segment}: speech match ${speech.match.toFixed(3)} < ${t.minSpeechMatch}${speech.unheard.length ? ` (unheard: ${speech.unheard.slice(0, 8).join(" ")})` : ""}`);
    }
  }
  return problems.length
    ? fail(name, problems.join("; "), { evidence })
    : pass(name, `every narrated segment matched its script at ${t.minSpeechMatch} or better`, { evidence });
}

export interface LayoutProblem {
  frame: number;
  segment: string;
  kind: string;
  text: string;
  other?: string;
  detail: string;
}

export function checkLayout(result: {
  frames: number[];
  textRuns: number;
  problems: LayoutProblem[];
  /** Frames in which no visible text was found at all. */
  emptyFrames?: number[];
}): CheckResult {
  const name = "layout";
  const describe = (p: LayoutProblem) => `frame ${p.frame} (${p.segment}): ${p.kind} "${p.text}"${p.other ? ` / "${p.other}"` : ""} — ${p.detail}`;
  const failing = result.problems.filter((p) => p.kind !== "truncated");
  const warnings = result.problems.filter((p) => p.kind === "truncated").map(describe);
  if (!result.frames.length || !result.textRuns) return fail(name, "no text was inspected", { evidence: result });
  // Every frame carries at least its mode label: a frame with no visible text was not laid out.
  if (result.emptyFrames?.length) return fail(name, `no visible text in frame(s) ${result.emptyFrames.join(", ")}`, { evidence: result });
  if (failing.length) return fail(name, `${failing.length} text problem(s): ${failing.slice(0, 5).map(describe).join("; ")}`, { warnings, evidence: result });
  return pass(name, `${result.textRuns} text runs over ${result.frames.length} frames: no overflow, clipping or overlap`, {
    ...(warnings.length ? { warnings } : {}),
    evidence: { frames: result.frames, textRuns: result.textRuns },
  });
}
