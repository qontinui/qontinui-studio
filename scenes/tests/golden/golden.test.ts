/**
 * Golden-frame and determinism gate for S1 (plan Phase 2 arming line):
 * - a visual change to S1 fails `matches the committed goldens`;
 * - two independent renders of one trace must be byte-identical.
 *
 * On failure, actual/diff images are in `.golden-out/` (CI uploads them).
 * Accept an intended change with `pnpm goldens:update` and commit the PNGs.
 */
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";
import { beforeAll, describe, expect, it } from "vitest";

import { RemotionAdapter } from "../../src/adapters/remotion/render";
import {
  GOLDEN_CASES,
  GOLDEN_DIR,
  GOLDEN_SCALE,
  MAX_DIFF_RATIO,
  OUT_DIR,
  PIXEL_THRESHOLD,
  SCENE_ID,
  casePrefix,
  goldenName,
  loadS1Trace,
} from "./cases";

async function renderAll(outDir: string): Promise<Map<string, string>> {
  const adapter = new RemotionAdapter();
  const files = new Map<string, string>();
  try {
    const trace = loadS1Trace();
    for (const { aspect, frames } of GOLDEN_CASES) {
      const written = await adapter.renderStills({
        scene: SCENE_ID,
        trace,
        aspect,
        frames,
        outDir,
        prefix: casePrefix(aspect),
        scale: GOLDEN_SCALE,
      });
      written.forEach((file) => files.set(path.basename(file), file));
    }
  } finally {
    await adapter.close();
  }
  return files;
}

const names = GOLDEN_CASES.flatMap(({ aspect, frames }) => frames.map((f) => goldenName(aspect, f)));
let first: Map<string, string>;
let second: Map<string, string>;

beforeAll(async () => {
  rmSync(OUT_DIR, { recursive: true, force: true });
  first = await renderAll(path.join(OUT_DIR, "actual"));
  second = await renderAll(path.join(OUT_DIR, "repeat"));
});

describe("S1 golden frames", () => {
  it.each(names)("%s matches the committed golden", (name) => {
    const goldenPath = path.join(GOLDEN_DIR, name);
    if (!existsSync(goldenPath)) {
      throw new Error(`missing golden ${name}; run \`pnpm goldens:update\` and commit it`);
    }
    const actualPath = first.get(name);
    if (!actualPath) throw new Error(`render produced no ${name}`);
    const golden = PNG.sync.read(readFileSync(goldenPath));
    const actual = PNG.sync.read(readFileSync(actualPath));
    expect([actual.width, actual.height], "dimensions").toEqual([golden.width, golden.height]);

    const diff = new PNG({ width: golden.width, height: golden.height });
    const mismatched = pixelmatch(golden.data, actual.data, diff.data, golden.width, golden.height, {
      threshold: PIXEL_THRESHOLD,
    });
    const ratio = mismatched / (golden.width * golden.height);
    if (mismatched > 0) {
      mkdirSync(path.join(OUT_DIR, "diff"), { recursive: true });
      writeFileSync(path.join(OUT_DIR, "diff", name), PNG.sync.write(diff));
    }
    expect(
      ratio,
      `${name}: ${mismatched} pixels differ (${(ratio * 100).toFixed(3)}%, limit ${(MAX_DIFF_RATIO * 100).toFixed(3)}%); see .golden-out/diff/${name}`,
    ).toBeLessThanOrEqual(MAX_DIFF_RATIO);
  });
});

describe("S1 determinism", () => {
  it.each(names)("%s is byte-identical across two independent renders", (name) => {
    const a = first.get(name);
    const b = second.get(name);
    if (!a || !b) throw new Error(`render produced no ${name}`);
    expect(readFileSync(a).equals(readFileSync(b)), `${name} differs between two renders of one trace`).toBe(true);
  });
});
