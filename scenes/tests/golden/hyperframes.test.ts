/**
 * HyperFrames adapter determinism (plan Phase 2, HyperFrames adapter spike;
 * docs/hyperframes-spike.md). Opt-in: runs only with HYPERFRAMES=1 and the
 * render image built (`pnpm hyperframes:image`), via `pnpm test:hyperframes`.
 * The default `pnpm test:golden` and CI skip it.
 *
 * - Run to run: two full png-sequence renders of S1 (16:9, 426 frames) are
 *   byte-identical, frame by frame.
 * - Worker count: a single-worker render equals a 4-worker one (explicit, so
 *   a small machine cannot end up comparing 1 worker with 1), so no frame
 *   depends on which frames its worker drew before it.
 * - Order: the five golden frame indices rendered as stills in suite order, in
 *   reversed order, and each ALONE in a fresh container equal the same frames
 *   of the full sequence.
 * - mp4: two renders have the same SHA-256.
 *
 * Cross-engine parity with the Remotion goldens is informative only and is
 * not asserted (antialiasing differs between the engines' raster paths).
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, rmSync } from "node:fs";
import path from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

import { HyperFramesAdapter } from "../../src/adapters/hyperframes/render";
import { AdapterClosed } from "../../src/api/engine";
import { GOLDEN_CASES, OUT_DIR, SCENE_ID, loadS1Trace } from "./cases";
import { interruptAfter, type InterruptKind } from "./interrupt";

const ENABLED = process.env.HYPERFRAMES === "1";
const HF_OUT = path.join(OUT_DIR, "hyperframes");
const FRAMES = (GOLDEN_CASES[0] as (typeof GOLDEN_CASES)[number]).frames;
const DURATION = 426;
const TIMEOUT_MS = 30 * 60_000;

const sha256 = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");

let first: string[] = [];
let second: string[] = [];
let oneWorker: string[] = [];
const stills = new Map<string, string[]>();
const mp4: string[] = [];

describe.skipIf(!ENABLED)("HyperFrames adapter: S1 determinism", () => {
  beforeAll(async () => {
    // .golden-out/ is the golden suites' scratch directory (git-ignored).
    rmSync(HF_OUT, { recursive: true, force: true });
    const trace = loadS1Trace();
    const sequence = async (adapter: HyperFramesAdapter, dir: string) =>
      (await adapter.render({ scene: SCENE_ID, trace, format: "png", aspect: "16:9", outPath: path.join(HF_OUT, dir) }))
        .files;
    const adapter = new HyperFramesAdapter();
    const fourWorkers = new HyperFramesAdapter({ workers: 4 });
    first = await sequence(fourWorkers, "seq-1");
    second = await sequence(fourWorkers, "seq-2");
    oneWorker = await sequence(new HyperFramesAdapter({ workers: 1 }), "seq-w1");

    const still = (frames: readonly number[], dir: string) =>
      adapter.renderStills({ scene: SCENE_ID, trace, aspect: "16:9", frames, outDir: path.join(HF_OUT, dir) });
    stills.set("suite", await still(FRAMES, "stills-suite"));
    stills.set("reversed", await still([...FRAMES].reverse(), "stills-reversed"));
    for (const frame of FRAMES) stills.set(`alone-${frame}`, await still([frame], `stills-alone-${frame}`));

    for (const run of ["a", "b"]) {
      const out = path.join(HF_OUT, `s1-${run}.mp4`);
      await adapter.render({ scene: SCENE_ID, trace, format: "mp4", aspect: "16:9", outPath: out });
      mp4.push(out);
    }
  }, TIMEOUT_MS);

  it("renders every frame of the scene", () => {
    expect(first).toHaveLength(DURATION);
    expect(second).toHaveLength(DURATION);
    expect(oneWorker).toHaveLength(DURATION);
  });

  it("is byte-identical run to run, frame by frame", () => {
    const differing = first.filter((file, i) => sha256(file) !== sha256(second[i] as string));
    expect(differing, "frames that differ between two renders").toEqual([]);
  });

  it("does not depend on the worker count (render history)", () => {
    const differing = first.filter((file, i) => sha256(file) !== sha256(oneWorker[i] as string));
    expect(differing, "frames that differ between a 4-worker and a single-worker render").toEqual([]);
  });

  it.each(FRAMES.map((f) => [f]))("frame %i: suite order, reversed order and alone all equal the sequence", (frame) => {
    const expected = sha256(first[frame] as string);
    const suite = stills.get("suite") as string[];
    const reversed = stills.get("reversed") as string[];
    expect(sha256(suite[FRAMES.indexOf(frame)] as string), "in suite order").toBe(expected);
    expect(sha256(reversed[[...FRAMES].reverse().indexOf(frame)] as string), "in reversed order").toBe(expected);
    expect(sha256((stills.get(`alone-${frame}`) as string[])[0] as string), "alone").toBe(expected);
  });

  it("two mp4 renders are byte-identical", () => {
    expect(sha256(mp4[1] as string)).toBe(sha256(mp4[0] as string));
  });
});

/**
 * close() mid-render (review F1/F2): at several delays — before the image
 * check, during `docker create`, during the render, between render and
 * publish — the render rejects with AdapterClosed, nothing is published
 * after close(), and no studio container is left behind.
 */
const INTERRUPTS: { kind: InterruptKind; delayMs: number }[] = [
  { kind: "mp4", delayMs: 0 },
  { kind: "mp4", delayMs: 150 },
  { kind: "mp4", delayMs: 400 },
  { kind: "mp4", delayMs: 1000 },
  { kind: "mp4", delayMs: 3000 },
  { kind: "png-sequence", delayMs: 6000 },
  { kind: "stills", delayMs: 2000 },
];

const studioContainers = () =>
  execFileSync("docker", ["ps", "-aq", "--filter", "label=org.qontinui.studio.run"], { encoding: "utf8" }).split(/\s+/).filter(Boolean);

describe.skipIf(!ENABLED)("HyperFrames adapter: close() mid-render", () => {
  it.each(INTERRUPTS)("$kind, close after $delayMs ms", async ({ kind, delayMs }) => {
    const outcome = await interruptAfter(new HyperFramesAdapter(), kind, delayMs);
    expect(outcome.error).toBeInstanceOf(AdapterClosed);
    expect(outcome.writtenAfterClose).toEqual([]);
    if (kind !== "stills") expect(outcome.entries).toEqual([]);
    expect(studioContainers(), "containers left after close()").toEqual([]);
  }, TIMEOUT_MS);
});
