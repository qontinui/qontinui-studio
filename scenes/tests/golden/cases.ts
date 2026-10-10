/**
 * The golden-frame set for S1, shared by the test and `pnpm goldens:update`.
 *
 * Frames are chosen to land on distinct visual states of the illustrative
 * trace (36-frame intro, then one beat every 30 frames):
 *   0   intro, every stage inert and fading in
 *   48  draft beat: first stage waiting, caption in
 *   196 CI failed: attention colour, gate closed, PR card "CI failed"
 *   290 merge train: landing colour, gate open
 *   425 last frame: every stage done
 * plus one mid-scene frame for each of the other two aspects, and one
 * frame per aspect of a replay with the longest allowed source (the mode
 * label must wrap, not clip, and must not collide with the header).
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { Aspect } from "../../src/api/layout";
import { parseTrace, type SceneTrace } from "../../src/api/trace/schema";
import { attestReplayForTests } from "../../src/api/trace/testing";

export const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
export const GOLDEN_DIR = path.join(PACKAGE_ROOT, "tests/golden/__goldens__");
export const OUT_DIR = path.join(PACKAGE_ROOT, ".golden-out");

export const SCENE_ID = "s1-plan-lifecycle";
export const TRACE_FILE = path.join(PACKAGE_ROOT, "traces/illustrative/s1-plan-lifecycle.json");

/** Goldens render at half size: enough to catch any visual change, small in git. */
export const GOLDEN_SCALE = 0.5;

/**
 * Goldens must match EXACTLY: zero differing pixels at pixelmatch threshold 0
 * (anti-aliased pixels counted). pixelmatch is used only to count and to draw
 * the diff image. A looser tolerance let real changes through: a header
 * recolour, and a one-digit PR number change (412 -> 413, 29 px).
 *
 * Exactness is affordable because renders have been byte-identical across
 * the machines tried so far: the Debian 13 development host and a local
 * ubuntu:24.04 container (both AVX2). That is not proof for every machine;
 * CI (ubuntu-24.04) is the authority. If it ever shows rasterisation noise,
 * the uploaded diff images are the evidence to decide on.
 */
export const PIXEL_THRESHOLD = 0;

export interface GoldenCase {
  /** File-name prefix for this case's goldens. */
  id: string;
  aspect: Aspect;
  frames: readonly number[];
  /** Builds the trace; may attest a test-only replay. */
  trace: () => SceneTrace;
}

/** 120 characters: SOURCE_REF_MAX, the longest source the schema accepts. */
export const LONG_SOURCE_REF =
  "build-record export of qontinui/qontinui-design-tokens, qontinui/qontinui-studio and qontinui/qontinui-web, last 30 days";

export function loadS1Trace(): SceneTrace {
  return parseTrace(JSON.parse(readFileSync(TRACE_FILE, "utf8")));
}

/**
 * The S1 events as a REPLAY with the longest allowed source, attested through
 * the test-only hook (the CLI refuses hand-written replays). Checks that the
 * mode label wraps instead of clipping at the narrowest aspect.
 */
export function longSourceReplayTrace(): SceneTrace {
  const json = JSON.parse(readFileSync(TRACE_FILE, "utf8"));
  json.meta = {
    mode: "replay",
    title: json.meta.title,
    source: { kind: "build-record", ref: LONG_SOURCE_REF, export_version: "1" },
    date_range: { from: "2026-09-14T00:00:00Z", to: "2026-09-15T23:59:59Z" },
  };
  return attestReplayForTests(parseTrace(json));
}

export const GOLDEN_CASES: readonly GoldenCase[] = [
  { id: `${SCENE_ID}-16x9`, aspect: "16:9", frames: [0, 48, 196, 290, 425], trace: loadS1Trace },
  { id: `${SCENE_ID}-9x16`, aspect: "9:16", frames: [250], trace: loadS1Trace },
  { id: `${SCENE_ID}-1x1`, aspect: "1:1", frames: [250], trace: loadS1Trace },
  { id: `${SCENE_ID}-16x9-replay-long-source`, aspect: "16:9", frames: [250], trace: longSourceReplayTrace },
  { id: `${SCENE_ID}-9x16-replay-long-source`, aspect: "9:16", frames: [250], trace: longSourceReplayTrace },
  { id: `${SCENE_ID}-1x1-replay-long-source`, aspect: "1:1", frames: [250], trace: longSourceReplayTrace },
];

export function goldenName(c: GoldenCase, frame: number): string {
  return `${c.id}-f${String(frame).padStart(4, "0")}.png`;
}
