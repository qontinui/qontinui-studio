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
 * pixels allowed to differ. Anti-aliased pixels are COUNTED (`includeAA`):
 * at golden scale most text pixels are anti-aliased edges, so excluding them
 * let a label colour change (text.muted -> text.secondary) pass unseen.
 *
 * The threshold is deliberately tight: S1's state colours are translucent
 * tints, and changing one stage from `attention` to `waiting` moves 1.97% of
 * the frame at 0.02 but only 0.14% at pixelmatch's default 0.1, which is too
 * close to the limit. The ratio is tight too (0.01%, ~50 pixels of a 960x540
 * frame), because recolouring one small label moves only ~0.1-0.2%. That is
 * affordable because the pinned browser + fontconfig give byte-identical
 * frames across machines (verified Debian 13 host vs Ubuntu 24.04 container,
 * both AVX2). If a CI CPU ever adds rasterisation noise, the diff artifact
 * shows it; widen this only with that evidence.
 */
export const PIXEL_THRESHOLD = 0.02;
export const MAX_DIFF_RATIO = 0.0001;

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
