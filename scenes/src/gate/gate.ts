/**
 * The unattended publish gate (plan Phase 5). Given a composed video and the
 * manifest beside it, it decides whether the video may be published with no
 * human looking at it, and records why:
 *
 * - **manifest**: the manifest parses, the video hashes to it, and the
 *   program rebuilt from its re-hashed inputs places every segment where it
 *   says;
 * - **speech**: every narrated segment's independent speech check matched
 *   its script at 0.9 or better (read from the timing table, re-hashed);
 * - **layout**: no text overflows the frame, is clipped, or overlaps other
 *   text, at three points in every segment (designed ellipsis truncation is
 *   a warning);
 * - **ffprobe**: the container, codecs, size, frame rate, pixel format and
 *   duration are what the manifest says;
 * - **frames**: four sampled frames, none black or flat;
 * - **audio**: there is narration audio, it is not silent, has no long
 *   silence and does not clip;
 * - **captions**: the captions file is valid WebVTT and covers every
 *   narrated segment.
 *
 * Every check runs (one failure does not hide another). The verdict passes
 * only when every check passes; a check that could not be measured fails.
 */
import { readFile } from "node:fs/promises";
import path from "node:path";

import { captionCues, toWebVtt } from "../api/captions";
import type { Program } from "../api/composition";
import type { Aspect } from "../api/layout";
import {
  DEFAULT_THRESHOLDS,
  checkAudio,
  checkCaptions,
  checkFrames,
  checkLayout,
  checkProbe,
  checkSpeech,
  samplePoints,
  type CheckResult,
  type GateThresholds,
  type LayoutProblem,
} from "./checks";
import { sha256 } from "./hash";
import { compositionManifest, rebuildProgram, type CompositionManifest, type RebuiltProgram } from "./manifest";
import { audioStats, frameStats, probe, type AudioStats, type FrameStats, type Probe } from "./probe";

export const GATE_VERSION = 1;

export interface GateVerdict {
  gate_version: number;
  video: string;
  manifest: string;
  verdict: "pass" | "fail";
  failed: string[];
  checks: CheckResult[];
  thresholds: GateThresholds;
  checked_at: string;
}

export type LayoutInspector = (
  program: Program,
  aspect: Aspect,
  frames: readonly number[],
  images: Readonly<Record<string, string>>,
) => Promise<{ frames: number[]; textRuns: number; problems: LayoutProblem[]; emptyFrames?: number[] }>;

export interface GateDeps {
  inspectLayout: LayoutInspector;
  probe?: (file: string) => Promise<Probe>;
  frameStats?: (file: string, at: number) => Promise<FrameStats>;
  audioStats?: (file: string, minSilence: number) => Promise<AudioStats>;
  now?: () => Date;
}

/** Three points in every segment: near its start, its middle, near its end. */
export function layoutFrames(program: Program): number[] {
  const frames = new Set<number>();
  for (const s of program.segments) {
    for (const f of [0.1, 0.5, 0.9]) frames.add(s.startFrame + Math.min(s.durationInFrames - 1, Math.floor(s.durationInFrames * f)));
  }
  return [...frames].sort((a, b) => a - b);
}

const message = (err: unknown) => (err instanceof Error ? err.message : String(err));

export async function runGate(videoPath: string, deps: GateDeps, thresholds: GateThresholds = DEFAULT_THRESHOLDS): Promise<GateVerdict> {
  const video = path.resolve(videoPath);
  const manifestPath = `${video}.composition.json`;
  const checks: CheckResult[] = [];
  const unmeasured = (name: string, err: unknown) => checks.push({ name, status: "fail", detail: `could not be measured: ${message(err)}` });

  // manifest
  let manifest: CompositionManifest | null = null;
  let rebuilt: RebuiltProgram | null = null;
  try {
    const parsed = compositionManifest.safeParse(JSON.parse(await readFile(manifestPath, "utf8")));
    if (!parsed.success) throw new Error(`the manifest is malformed: ${parsed.error.issues[0]?.path.join(".")}: ${parsed.error.issues[0]?.message}`);
    manifest = parsed.data;
    if (path.basename(video) !== manifest.output.file) throw new Error(`the manifest describes ${manifest.output.file}, not ${path.basename(video)}`);
    if (sha256(await readFile(video)) !== manifest.output.sha256) throw new Error("the video does not hash to the manifest's output.sha256");
    rebuilt = await rebuildProgram(manifest);
    checks.push({
      name: "manifest",
      status: "pass",
      detail: `${manifest.composition}, ${manifest.segments.length} segments (${manifest.omitted.length} omitted); every input re-hashed and the program rebuilt`,
      ...(manifest.omitted.length ? { warnings: manifest.omitted.map((o) => `omitted ${o.id}: ${o.reason}`) } : {}),
    });
  } catch (err) {
    checks.push({ name: "manifest", status: "fail", detail: message(err) });
  }
  const duration = manifest ? manifest.duration_in_frames / manifest.fps : 0;

  // The narration clips as the REBUILT program places them; the manifest's
  // own list must agree, but is never what the checks iterate.
  const clips = (rebuilt?.program.audio ?? []).map((c) => ({ segment: c.segmentId, start_frame: c.startFrame, duration_in_frames: c.durationInFrames }));
  const listed = (manifest?.narration?.clips ?? []).map((c) => ({ segment: c.segment, start_frame: c.start_frame, duration_in_frames: c.duration_in_frames }));
  const clipsAgree = JSON.stringify(clips) === JSON.stringify(listed);

  // speech
  if (!rebuilt) checks.push({ name: "speech", status: "fail", detail: "the program could not be rebuilt from the manifest" });
  else if (!clipsAgree) checks.push({ name: "speech", status: "fail", detail: "the manifest's narration clips are not the ones the rebuilt program plays" });
  else checks.push(checkSpeech(clips, rebuilt.timing, thresholds));

  // layout
  if (rebuilt) {
    try {
      checks.push(checkLayout(await deps.inspectLayout(rebuilt.program, rebuilt.aspect, layoutFrames(rebuilt.program), rebuilt.images)));
    } catch (err) {
      unmeasured("layout", err);
    }
  } else checks.push({ name: "layout", status: "fail", detail: "the program could not be rebuilt from the manifest" });

  // ffprobe
  let probed: Probe | null = null;
  try {
    probed = await (deps.probe ?? probe)(video);
    checks.push(manifest ? checkProbe(manifest, probed) : { name: "ffprobe", status: "fail", detail: "no readable manifest to compare with" });
  } catch (err) {
    unmeasured("ffprobe", err);
  }

  // frames
  try {
    const at = samplePoints(duration || Number(probed?.format?.duration ?? 0));
    const stats: FrameStats[] = [];
    for (const t of at) stats.push(await (deps.frameStats ?? frameStats)(video, t));
    checks.push(checkFrames(stats, thresholds));
  } catch (err) {
    unmeasured("frames", err);
  }

  // audio
  try {
    const hasAudio = (probed?.streams ?? []).some((s) => s.codec_type === "audio");
    const stats = hasAudio ? await (deps.audioStats ?? audioStats)(video, thresholds.maxSilenceSeconds) : null;
    checks.push(checkAudio(stats, duration, thresholds));
  } catch (err) {
    unmeasured("audio", err);
  }

  // captions
  if (manifest && rebuilt && clipsAgree) {
    let vtt: string | null = null;
    if (manifest.captions) vtt = await readFile(path.join(path.dirname(video), manifest.captions.file), "utf8").catch(() => null);
    const cues = rebuilt.timing ? captionCues(rebuilt.program, rebuilt.timing) : [];
    checks.push(checkCaptions(manifest, clips, vtt, cues.length ? toWebVtt(cues) : null, duration));
  } else checks.push({ name: "captions", status: "fail", detail: "no rebuilt program (or its clips disagree with the manifest) to check captions against" });

  const failed = checks.filter((c) => c.status === "fail").map((c) => c.name);
  return {
    gate_version: GATE_VERSION,
    video,
    manifest: manifestPath,
    verdict: failed.length ? "fail" : "pass",
    failed,
    checks,
    thresholds,
    checked_at: (deps.now ?? (() => new Date()))().toISOString(),
  };
}
