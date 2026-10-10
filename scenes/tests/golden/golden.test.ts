/**
 * Golden-frame and determinism gate for S1 (plan Phase 2 arming line):
 * - a visual change to S1 fails `matches the committed golden` (exact match);
 * - two independent renders of one trace must be byte-identical.
 *
 * On failure, actual/diff images are in `.golden-out/` (CI uploads them).
 * Accept an intended change with `pnpm goldens:update` (this suite with
 * UPDATE_GOLDENS=1), inspect the PNGs, and commit them.
 */
import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";

import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";
import { beforeAll, describe, expect, it } from "vitest";

import { RemotionAdapter } from "../../src/adapters/remotion/render";
import { GOLDEN_CASES, GOLDEN_DIR, GOLDEN_DOWNSAMPLE, OUT_DIR, PIXEL_THRESHOLD, SCENE_ID, goldenName } from "./cases";

const UPDATE = process.env.UPDATE_GOLDENS === "1";

async function renderAll(outDir: string): Promise<Map<string, string>> {
  const adapter = new RemotionAdapter();
  const files = new Map<string, string>();
  try {
    for (const c of GOLDEN_CASES) {
      const written = await adapter.renderStills({
        scene: SCENE_ID,
        trace: c.trace(),
        aspect: c.aspect,
        frames: c.frames,
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
let first: Map<string, string>;
let second: Map<string, string>;
const reducedFiles = new Map<string, string>();

beforeAll(async () => {
  // .golden-out/ is this suite's own scratch directory (git-ignored).
  rmSync(OUT_DIR, { recursive: true, force: true });
  // Full-size renders (the determinism test compares these byte for byte)...
  first = await renderAll(path.join(OUT_DIR, "full"));
  second = await renderAll(path.join(OUT_DIR, "full-repeat"));
  // ...reduced to golden size for the golden comparison.
  mkdirSync(path.join(OUT_DIR, "actual"), { recursive: true });
  for (const name of names) {
    const reduced = downsample(PNG.sync.read(readFileSync(first.get(name) as string)), GOLDEN_DOWNSAMPLE);
    const file = path.join(OUT_DIR, "actual", name);
    writeFileSync(file, PNG.sync.write(reduced));
    reducedFiles.set(name, file);
  }
  if (UPDATE) {
    mkdirSync(GOLDEN_DIR, { recursive: true });
    // Remove only stale golden PNGs, file by file.
    for (const name of readdirSync(GOLDEN_DIR)) {
      if (name.endsWith(".png") && !names.includes(name)) unlinkSync(path.join(GOLDEN_DIR, name));
    }
    for (const name of names) copyFileSync(reducedFiles.get(name) as string, path.join(GOLDEN_DIR, name));
    process.stdout.write(`updated ${names.length} goldens in ${GOLDEN_DIR}\n`);
  }
});

describe("S1 golden frames", () => {
  it.each(names)("%s matches the committed golden exactly", (name) => {
    const goldenPath = path.join(GOLDEN_DIR, name);
    if (!existsSync(goldenPath)) {
      throw new Error(`missing golden ${name}; run \`pnpm goldens:update\` and commit it`);
    }
    const actualPath = reducedFiles.get(name);
    if (!actualPath) throw new Error(`render produced no ${name}`);
    const golden = PNG.sync.read(readFileSync(goldenPath));
    const actual = PNG.sync.read(readFileSync(actualPath));
    expect([actual.width, actual.height], "dimensions").toEqual([golden.width, golden.height]);

    // pixelmatch only counts and draws the diff; the gate is "zero pixels differ".
    const diff = new PNG({ width: golden.width, height: golden.height });
    const mismatched = pixelmatch(golden.data, actual.data, diff.data, golden.width, golden.height, {
      threshold: PIXEL_THRESHOLD,
      includeAA: true,
    });
    if (mismatched > 0) {
      mkdirSync(path.join(OUT_DIR, "diff"), { recursive: true });
      writeFileSync(path.join(OUT_DIR, "diff", name), PNG.sync.write(diff));
    }
    expect(mismatched, `${name}: ${mismatched} pixels differ from the golden; see .golden-out/diff/${name}`).toBe(0);
  });
});

describe("S1 determinism", () => {
  it.each(names)("%s is byte-identical across two independent full-size renders", (name) => {
    const a = first.get(name);
    const b = second.get(name);
    if (!a || !b) throw new Error(`render produced no ${name}`);
    expect(readFileSync(a).equals(readFileSync(b)), `${name} differs between two renders of one trace`).toBe(true);
  });
});
