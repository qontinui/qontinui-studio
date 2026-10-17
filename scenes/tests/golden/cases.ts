/**
 * The golden-frame set, shared by the test and `pnpm goldens:update`. Every
 * registered scene has cases at all three aspects (a unit test checks it).
 *
 * S1's frames are chosen to land on distinct visual states of the illustrative
 * trace (36-frame intro, then one beat every 30 frames):
 *   0   intro, every stage inert and fading in
 *   48  draft beat: first stage waiting, caption in
 *   196 CI failed: attention colour, gate closed, PR card "CI failed"
 *   290 merge train: landing colour, gate open
 *   425 last frame: every stage done
 * plus one mid-scene frame for each of the other two aspects, and one
 * frame per aspect of a replay with the longest allowed source (the mode
 * label must wrap, not clip, and must not collide with the header); a
 * replay whose source is 120 "W"s (the label clamps to three lines) and a
 * 160-character title (clamped to two lines) at 9:16 and 1:1.
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

/** A scene's committed illustrative trace (`traces/illustrative/<scene>.json`). */
export function illustrativeTraceFile(scene: string): string {
  return path.join(PACKAGE_ROOT, "traces/illustrative", `${scene}.json`);
}

/** The committed illustrative trace of `scene`, validated. */
export function loadIllustrativeTrace(scene: string): SceneTrace {
  return parseTrace(JSON.parse(readFileSync(illustrativeTraceFile(scene), "utf8")));
}

export const TRACE_FILE = illustrativeTraceFile(SCENE_ID);

/**
 * Goldens are rendered at native size (scale 1) and then reduced 2x2 by a
 * box filter in JavaScript, so they stay small in git.
 *
 * Why not render at --scale 0.5 directly: at a fractional device scale one
 * edge pixel of the S1 gate came out ±1 in one channel in about 1 frame in 20
 * to 40 across repeated renders, even after the blur fix (src/api/glow.ts).
 * At scale 1 it does not. The CLI therefore refuses fractional scales unless
 * --allow-nondeterministic is given.
 */
export const GOLDEN_DOWNSAMPLE = 2;

/**
 * The golden gate is the full-size frame's SHA-256 (exact). The reduced PNG
 * beside it is for humans; on a mismatch it is diffed at pixelmatch
 * threshold 0 (anti-aliased pixels counted) only to draw the diff image.
 *
 * Exactness is affordable because full-size renders have been byte-identical
 * on the machines tried so far: the Debian 13 development host and a local
 * ubuntu:24.04 container (both AVX2). That is not proof for every machine;
 * CI (ubuntu-24.04) is the authority.
 */
export const PIXEL_THRESHOLD = 0;

export interface GoldenCase {
  /** File-name prefix for this case's goldens. */
  id: string;
  /** Scene id from the registry. */
  scene: string;
  aspect: Aspect;
  frames: readonly number[];
  /** Builds the trace; may attest a test-only replay. */
  trace: () => SceneTrace;
  /** Also render each of these frames alone in a fresh browser (order test). */
  alone?: boolean;
}

/**
 * The cases every new scene starts with: its illustrative trace at each
 * aspect, the 16:9 frames rendered alone too, plus one 9:16 and one 1:1
 * frame alone.
 */
export function illustrativeCases(
  scene: string,
  frames: { "16:9": readonly number[]; "9:16": readonly number[]; "1:1": readonly number[] },
): GoldenCase[] {
  const trace = () => loadIllustrativeTrace(scene);
  return [
    { id: `${scene}-16x9`, scene, aspect: "16:9", frames: frames["16:9"], trace, alone: true },
    { id: `${scene}-9x16`, scene, aspect: "9:16", frames: frames["9:16"], trace, alone: true },
    { id: `${scene}-1x1`, scene, aspect: "1:1", frames: frames["1:1"], trace, alone: true },
  ];
}

/** 120 characters: SOURCE_REF_MAX, the longest source the schema accepts. */
export const LONG_SOURCE_REF =
  "build-record export of qontinui/qontinui-design-tokens, qontinui/qontinui-studio and qontinui/qontinui-web, last 30 days";

export function loadS1Trace(): SceneTrace {
  return loadIllustrativeTrace(SCENE_ID);
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

/** 160 characters: the schema's maximum plan title. */
export const LONG_TITLE =
  "Replace the sync worker's unbounded retry loop with a per-tenant retry budget, report budget exhaustion as a typed alert, and backfill metrics for every new job";

/** A replay whose source is 120 "W"s: the widest glyph, no break opportunities. */
export function wideSourceReplayTrace(): SceneTrace {
  const json = JSON.parse(readFileSync(TRACE_FILE, "utf8"));
  json.meta = {
    mode: "replay",
    title: json.meta.title,
    source: { kind: "build-record", ref: "W".repeat(120), export_version: "1" },
    date_range: { from: "2026-09-14T00:00:00Z", to: "2026-09-15T23:59:59Z" },
  };
  return attestReplayForTests(parseTrace(json));
}

/** The illustrative trace with a 160-character plan title. */
export function longTitleTrace(): SceneTrace {
  const json = JSON.parse(readFileSync(TRACE_FILE, "utf8"));
  json.entities.plans[0].title = LONG_TITLE;
  return parseTrace(json);
}

const S1_CASES: readonly GoldenCase[] = [
  ...illustrativeCases(SCENE_ID, { "16:9": [0, 48, 196, 290, 425], "9:16": [250], "1:1": [250] }),
  ...(
    [
      ["16x9-replay-long-source", "16:9", longSourceReplayTrace],
      ["9x16-replay-long-source", "9:16", longSourceReplayTrace],
      ["1x1-replay-long-source", "1:1", longSourceReplayTrace],
      ["9x16-replay-wide-source", "9:16", wideSourceReplayTrace],
      ["1x1-replay-wide-source", "1:1", wideSourceReplayTrace],
      ["9x16-long-title", "9:16", longTitleTrace],
      ["1x1-long-title", "1:1", longTitleTrace],
    ] as const
  ).map(([suffix, aspect, trace]) => ({ id: `${SCENE_ID}-${suffix}`, scene: SCENE_ID, aspect, frames: [250], trace })),
];

export const GOLDEN_CASES: readonly GoldenCase[] = [...S1_CASES];

export function goldenName(c: GoldenCase, frame: number): string {
  return `${c.id}-f${String(frame).padStart(4, "0")}.png`;
}
