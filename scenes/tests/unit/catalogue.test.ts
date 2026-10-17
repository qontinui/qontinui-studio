/**
 * Every registered scene is complete (plan Phase 3, per scene): a committed
 * illustrative trace that drives it, a default trace in the engine root,
 * golden frames at all three aspects, and a poster frame inside its duration.
 * The scene matrix (tests/matrix) then renders and embeds each one.
 */
import { existsSync, readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { defaultTraces } from "../../src/adapters/remotion/Root";
import { ASPECTS } from "../../src/api/layout";
import { missingRequirements } from "../../src/api/scene";
import { posterFrame, sceneDuration } from "../../src/api/scene-root";
import { renderRefusal } from "../../src/api/trace/provenance";
import { parseTrace } from "../../src/api/trace/schema";
import { scenes } from "../../src/scenes";
import { GOLDEN_CASES, illustrativeTraceFile } from "../golden/cases";

const SCENE_IDS = Object.keys(scenes).sort();

describe.each(SCENE_IDS)("scene %s", (id) => {
  const scene = scenes[id]!;
  const file = illustrativeTraceFile(id);

  it("ships an illustrative trace that drives it", () => {
    expect(existsSync(file), `${file} is missing`).toBe(true);
    const trace = parseTrace(JSON.parse(readFileSync(file, "utf8")));
    expect(trace.meta.mode).toBe("illustrative");
    expect(trace.meta.source).toEqual({ kind: "hand-authored", ref: `traces/illustrative/${id}.json` });
    expect(renderRefusal(trace)).toBeNull();
    expect(missingRequirements(scene, trace)).toEqual([]);
  });

  it("is the engine root's default trace", () => {
    expect(defaultTraces[id]).toEqual(JSON.parse(readFileSync(file, "utf8")));
  });

  it("has golden frames at every aspect", () => {
    const aspects = new Set(GOLDEN_CASES.filter((c) => c.scene === id).map((c) => c.aspect));
    expect([...aspects].sort()).toEqual([...ASPECTS].sort());
  });

  it("has a poster frame inside its duration", () => {
    const trace = parseTrace(JSON.parse(readFileSync(file, "utf8")));
    const poster = posterFrame(scene, trace);
    expect(Number.isInteger(poster)).toBe(true);
    expect(poster).toBeGreaterThanOrEqual(0);
    expect(poster).toBeLessThan(sceneDuration(scene, trace));
  });
});

describe("the registry", () => {
  it("has no default trace for a scene it does not know", () => {
    expect(Object.keys(defaultTraces).sort()).toEqual(SCENE_IDS);
  });
});
