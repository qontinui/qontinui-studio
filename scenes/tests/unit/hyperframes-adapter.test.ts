/**
 * HyperFrames adapter refusals that need no Docker. Every case is refused
 * before any docker command runs: the adapter is given a docker binary that
 * does not exist, so reaching docker would fail with ENOENT instead of the
 * refusal asserted here.
 */
import { mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { imageRefProblem } from "../../src/adapters/hyperframes/image";
import { writeProject } from "../../src/adapters/hyperframes/project";
import { HyperFramesAdapter, bindMount } from "../../src/adapters/hyperframes/render";
import { REPLAY_REFUSAL } from "../../src/api/trace/provenance";
import { parseTrace } from "../../src/api/trace/schema";
import { replayTraceJson, s1TraceJson } from "./fixtures";

const NO_DOCKER = "/nonexistent/studio-test-no-docker";
const adapter = () => new HyperFramesAdapter({ docker: NO_DOCKER, image: "studio-hyperframes:0.8.78" });
const s1 = () => parseTrace(s1TraceJson());
/** Shape-valid, but hand-written: no build-record provenance. */
const handWrittenReplay = () => parseTrace(replayTraceJson());
const tmp = () => mkdtempSync(path.join(tmpdir(), "studio-hf-unit-"));

describe("HyperFramesAdapter.render refusals (no Docker)", () => {
  it("refuses a hand-written replay", async () => {
    const out = path.join(tmp(), "x.mp4");
    await expect(
      adapter().render({ scene: "s1-plan-lifecycle", trace: handWrittenReplay(), format: "mp4", aspect: "16:9", outPath: out }),
    ).rejects.toThrow(REPLAY_REFUSAL);
  });

  it("refuses scale 0.5", async () => {
    const out = path.join(tmp(), "x.mp4");
    await expect(
      adapter().render({ scene: "s1-plan-lifecycle", trace: s1(), format: "mp4", aspect: "16:9", outPath: out, scale: 0.5 }),
    ).rejects.toThrow(/renders at scale 1 only \(got 0\.5\)/);
  });

  it("refuses an existing target without overwrite", async () => {
    const out = path.join(tmp(), "x.mp4");
    writeFileSync(out, "keep me");
    await expect(
      adapter().render({ scene: "s1-plan-lifecycle", trace: s1(), format: "mp4", aspect: "16:9", outPath: out }),
    ).rejects.toThrow(/refusing to overwrite .*x\.mp4: it exists/);
  });
});

describe("HyperFramesAdapter.renderStills refusals (no Docker)", () => {
  const stills = (dir: string, extra: { trace?: ReturnType<typeof s1>; scale?: number } = {}) =>
    adapter().renderStills({
      scene: "s1-plan-lifecycle",
      trace: extra.trace ?? s1(),
      aspect: "16:9",
      frames: [196],
      outDir: dir,
      scale: extra.scale,
    });

  it("refuses a hand-written replay", async () => {
    await expect(stills(tmp(), { trace: handWrittenReplay() })).rejects.toThrow(REPLAY_REFUSAL);
  });

  it("refuses scale 0.5", async () => {
    await expect(stills(tmp(), { scale: 0.5 })).rejects.toThrow(/renders at scale 1 only \(got 0\.5\)/);
  });

  it("refuses an existing target without overwrite", async () => {
    const dir = tmp();
    writeFileSync(path.join(dir, "f0196.png"), "keep me");
    await expect(stills(dir)).rejects.toThrow(/refusing to overwrite .*f0196\.png: it exists/);
  });

  it("returns [] for no frames, without touching docker", async () => {
    await expect(
      adapter().renderStills({ scene: "s1-plan-lifecycle", trace: s1(), aspect: "16:9", frames: [], outDir: tmp() }),
    ).resolves.toEqual([]);
  });
});

describe("writeProject", () => {
  it("refuses a hand-written replay and writes nothing", async () => {
    const dir = tmp();
    await expect(
      writeProject(dir, { sceneId: "s1-plan-lifecycle", trace: handWrittenReplay(), aspect: "16:9", frames: [0] }),
    ).rejects.toThrow(REPLAY_REFUSAL);
    expect(readdirSync(dir)).toEqual([]);
  });
});

describe("container inputs", () => {
  it("refuses an image reference that starts with '-' or is not a reference", () => {
    expect(imageRefProblem("--privileged")).toMatch(/starts with "-"/);
    expect(imageRefProblem("studio hyperframes")).toMatch(/not a docker image reference/);
    expect(() => new HyperFramesAdapter({ image: "-v/:/host" })).toThrow(/starts with "-"/);
  });

  it("accepts tags, registry paths, digests and image ids", () => {
    for (const ref of [
      "studio-hyperframes:0.8.78",
      "ghcr.io/qontinui/studio-hyperframes:0.8.78",
      "localhost:5000/studio-hyperframes@sha256:" + "a".repeat(64),
      "sha256:" + "b".repeat(64),
    ]) {
      expect(imageRefProblem(ref), ref).toBeNull();
    }
  });

  it("refuses a bind-mount path with a comma or quote", () => {
    expect(() => bindMount("/tmp/a,dst=/etc", "/output", false)).toThrow(/contains a comma, quote or newline/);
    expect(() => bindMount('/tmp/a"b', "/output", false)).toThrow(/contains a comma, quote or newline/);
    expect(bindMount("/tmp/a:b", "/output", true)).toBe("type=bind,src=/tmp/a:b,dst=/output,readonly");
  });
});
