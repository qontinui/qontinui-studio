/**
 * Golden-frame, determinism and order-independence gate for S1 (plan Phase 2
 * arming line).
 *
 * - Golden: each full-size frame's SHA-256 must equal the committed
 *   `__goldens__/<name>.sha256`. A 2x2-reduced PNG is kept beside it for
 *   humans; on a mismatch the reduced diff image goes to `.golden-out/diff/`.
 * - Determinism: a second render with a fresh browser is byte-identical.
 * - Order independence: a frame rendered ALONE in a fresh browser, and the
 *   same frame rendered inside the suite order and inside the reversed order,
 *   are byte-identical. (A blurred glow once made frame 196 depend on whether
 *   frame 48 had been rendered first; see src/api/glow.ts.)
 *
 * Accept an intended change with `pnpm goldens:update` (UPDATE_GOLDENS=1),
 * inspect the reduced PNGs, and commit them with their .sha256 files.
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";

import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";
import { beforeAll, describe, expect, it } from "vitest";

import { RemotionAdapter } from "../../src/adapters/remotion/render";
import {
  GOLDEN_CASES,
  GOLDEN_DIR,
  GOLDEN_DOWNSAMPLE,
  OUT_DIR,
  PIXEL_THRESHOLD,
  SCENE_ID,
  type GoldenCase,
  goldenName,
} from "./cases";

const UPDATE = process.env.UPDATE_GOLDENS === "1";

/** Frames rendered alone, each in a fresh browser, for the order test. */
const ALONE: readonly { caseIndex: number; frame: number }[] = (() => {
  const c = GOLDEN_CASES[0] as GoldenCase;
  return c.frames.map((frame) => ({ caseIndex: 0, frame }));
})();

const sha256 = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");

async function renderCases(
  serveUrl: string,
  outDir: string,
  plan: readonly { c: GoldenCase; frames: readonly number[] }[],
): Promise<Map<string, string>> {
  const adapter = new RemotionAdapter({ serveUrl });
  const files = new Map<string, string>();
  try {
    for (const { c, frames } of plan) {
      const written = await adapter.renderStills({
        scene: SCENE_ID,
        trace: c.trace(),
        aspect: c.aspect,
        frames,
        outDir,
        prefix: `${c.id}-`,
        scale: 1,
      });
      written.forEach((file) => files.set(path.basename(file), file));
    }
  } finally {
    await adapter.close();
  }
  return files;
}

/** Deterministic NxN box-filter reduction (integer arithmetic, rounded). */
function downsample(png: PNG, factor: number): PNG {
  const width = Math.floor(png.width / factor);
  const height = Math.floor(png.height / factor);
  const out = new PNG({ width, height });
  const n = factor * factor;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      for (let c = 0; c < 4; c++) {
        let sum = 0;
        for (let dy = 0; dy < factor; dy++) {
          for (let dx = 0; dx < factor; dx++) {
            sum += png.data[((y * factor + dy) * png.width + (x * factor + dx)) * 4 + c] as number;
          }
        }
        out.data[(y * width + x) * 4 + c] = Math.round(sum / n);
      }
    }
  }
  return out;
}

const names = GOLDEN_CASES.flatMap((c) => c.frames.map((f) => goldenName(c, f)));
const suiteOrder = GOLDEN_CASES.map((c) => ({ c, frames: c.frames }));
const reversedOrder = [...GOLDEN_CASES].reverse().map((c) => ({ c, frames: [...c.frames].reverse() }));

let first: Map<string, string>;
let second: Map<string, string>;
let reversed: Map<string, string>;
const alone = new Map<string, string>();
const reducedFiles = new Map<string, string>();

beforeAll(async () => {
  // .golden-out/ is this suite's own scratch directory (git-ignored).
  rmSync(OUT_DIR, { recursive: true, force: true });
  const serveUrl = await RemotionAdapter.bundle();
  first = await renderCases(serveUrl, path.join(OUT_DIR, "full"), suiteOrder);
  second = await renderCases(serveUrl, path.join(OUT_DIR, "full-repeat"), suiteOrder);
  reversed = await renderCases(serveUrl, path.join(OUT_DIR, "full-reversed"), reversedOrder);
  for (const { caseIndex, frame } of ALONE) {
    const c = GOLDEN_CASES[caseIndex] as GoldenCase;
    const files = await renderCases(serveUrl, path.join(OUT_DIR, `full-alone-${frame}`), [{ c, frames: [frame] }]);
    const name = goldenName(c, frame);
    alone.set(name, files.get(name) as string);
  }

  mkdirSync(path.join(OUT_DIR, "actual"), { recursive: true });
  for (const name of names) {
    const reduced = downsample(PNG.sync.read(readFileSync(first.get(name) as string)), GOLDEN_DOWNSAMPLE);
    const file = path.join(OUT_DIR, "actual", name);
    writeFileSync(file, PNG.sync.write(reduced));
    reducedFiles.set(name, file);
  }

  if (UPDATE) {
    mkdirSync(GOLDEN_DIR, { recursive: true });
    const keep = new Set(names.flatMap((n) => [n, `${n}.sha256`]));
    // Remove only stale golden files, one by one.
    for (const name of readdirSync(GOLDEN_DIR)) {
      if ((name.endsWith(".png") || name.endsWith(".sha256")) && !keep.has(name)) unlinkSync(path.join(GOLDEN_DIR, name));
    }
    for (const name of names) {
      writeFileSync(path.join(GOLDEN_DIR, name), readFileSync(reducedFiles.get(name) as string));
      writeFileSync(path.join(GOLDEN_DIR, `${name}.sha256`), `${sha256(first.get(name) as string)}\n`);
    }
    process.stdout.write(`updated ${names.length} goldens (+ .sha256) in ${GOLDEN_DIR}\n`);
  }
});

describe("S1 golden frames", () => {
  it.each(names)("%s matches the committed golden exactly (full-size SHA-256)", (name) => {
    const hashPath = path.join(GOLDEN_DIR, `${name}.sha256`);
    const goldenPath = path.join(GOLDEN_DIR, name);
    if (!existsSync(hashPath) || !existsSync(goldenPath)) {
      throw new Error(`missing golden ${name}(.sha256); run \`pnpm goldens:update\` and commit it`);
    }
    const expected = readFileSync(hashPath, "utf8").trim();
    const actualHash = sha256(first.get(name) as string);
    if (actualHash === expected) return;

    // Mismatch: draw the reduced diff for a human, then fail.
    const golden = PNG.sync.read(readFileSync(goldenPath));
    const actual = PNG.sync.read(readFileSync(reducedFiles.get(name) as string));
    let mismatched = -1;
    if (actual.width === golden.width && actual.height === golden.height) {
      const diff = new PNG({ width: golden.width, height: golden.height });
      mismatched = pixelmatch(golden.data, actual.data, diff.data, golden.width, golden.height, {
        threshold: PIXEL_THRESHOLD,
        includeAA: true,
      });
      mkdirSync(path.join(OUT_DIR, "diff"), { recursive: true });
      writeFileSync(path.join(OUT_DIR, "diff", name), PNG.sync.write(diff));
    }
    throw new Error(
      `${name}: full-size SHA-256 ${actualHash.slice(0, 12)} != golden ${expected.slice(0, 12)}; ` +
        `${mismatched >= 0 ? `${mismatched} pixels differ at golden size` : "size changed"}; see .golden-out/diff/${name}`,
    );
  });
});

describe("S1 determinism", () => {
  it.each(names)("%s is byte-identical across two fresh-browser renders", (name) => {
    expect(sha256(second.get(name) as string), `${name} differs between two renders of one trace`).toBe(
      sha256(first.get(name) as string),
    );
  });
});

describe("S1 order independence", () => {
  it.each(names)("%s is byte-identical in suite order and in reversed order", (name) => {
    expect(sha256(reversed.get(name) as string), `${name} depends on render order`).toBe(sha256(first.get(name) as string));
  });

  it.each([...ALONE].map(({ caseIndex, frame }) => goldenName(GOLDEN_CASES[caseIndex] as GoldenCase, frame)))(
    "%s rendered alone in a fresh browser equals it inside the suite order",
    (name) => {
      expect(sha256(alone.get(name) as string), `${name} depends on what was rendered before it`).toBe(
        sha256(first.get(name) as string),
      );
    },
  );
});
