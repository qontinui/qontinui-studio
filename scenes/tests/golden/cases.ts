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
 * plus one mid-scene frame for each of the other two aspects.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { Aspect } from "../../src/api/layout";
import { parseTrace, type SceneTrace } from "../../src/api/trace/schema";

export const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
export const GOLDEN_DIR = path.join(PACKAGE_ROOT, "tests/golden/__goldens__");
export const OUT_DIR = path.join(PACKAGE_ROOT, ".golden-out");

export const SCENE_ID = "s1-plan-lifecycle";
export const TRACE_FILE = path.join(PACKAGE_ROOT, "traces/illustrative/s1-plan-lifecycle.json");

/** Goldens render at half size: enough to catch any visual change, small in git. */
export const GOLDEN_SCALE = 0.5;

/**
 * pixelmatch per-pixel colour threshold (0..1, YIQ distance) and the share of
 * pixels allowed to differ. Anti-aliased pixels are excluded by pixelmatch.
 * The ratio absorbs sub-pixel text rasterisation differences between
 * machines; any real change to a colour, layout or label exceeds it by
 * orders of magnitude.
 */
export const PIXEL_THRESHOLD = 0.1;
export const MAX_DIFF_RATIO = 0.001;

export interface GoldenCase {
  aspect: Aspect;
  frames: readonly number[];
}

export const GOLDEN_CASES: readonly GoldenCase[] = [
  { aspect: "16:9", frames: [0, 48, 196, 290, 425] },
  { aspect: "9:16", frames: [250] },
  { aspect: "1:1", frames: [250] },
];

export function casePrefix(aspect: Aspect): string {
  return `${SCENE_ID}-${aspect.replace(":", "x")}-`;
}

export function goldenName(aspect: Aspect, frame: number): string {
  return `${casePrefix(aspect)}f${String(frame).padStart(4, "0")}.png`;
}

export function loadS1Trace(): SceneTrace {
  return parseTrace(JSON.parse(readFileSync(TRACE_FILE, "utf8")));
}
