import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { STUDIO_FPS, findBeat, missingRequirements, parseTrace, validateTrace, type SceneTraceInput } from "../../src/api";
import { posterFrame } from "../../src/api/scene-root";
import { s6SelfCorrection } from "../../src/scenes/s6-self-correction";
import { s6State, s6Timeline } from "../../src/scenes/s6-self-correction/model";

const here = path.dirname(fileURLToPath(import.meta.url));
const traceJson = (): SceneTraceInput =>
  JSON.parse(readFileSync(path.join(here, "../../traces/illustrative/s6-self-correction.json"), "utf8")) as SceneTraceInput;
const errorsOf = (input: unknown) => {
  const result = validateTrace(input);
  return result.ok ? [] : result.errors;
};

describe("S6 trace fields", () => {
  it("accepts the committed illustrative trace and it drives the scene", () => {
    expect(missingRequirements(s6SelfCorrection, parseTrace(traceJson()))).toEqual([]);
  });

  it("refuses a diagnosis before any red run, and a fix before any diagnosis", () => {
    const json = traceJson();
    json.events[3]!.at = "2026-09-18T14:10:00Z";
    json.events[4]!.at = "2026-09-18T14:09:00Z";
    const errors = errorsOf(json);
    expect(errors).toContain('events.3.pr: ci.diagnosed before any ci.failed for "pr-430"');
    expect(errors).toContain('events.4.pr: fix.pushed before any ci.diagnosed for "pr-430"');
  });

  it("follows exactly one PR", () => {
    const json = traceJson();
    json.entities!.prs!.push({ id: "pr-2", plan: "plan-retry", repo: "example/x", number: 2, title: "t" });
    expect(missingRequirements(s6SelfCorrection, parseTrace(json))).toContain("exactly one PR (the trace has 2)");
  });
});

describe("S6 scene model", () => {
  const trace = parseTrace(traceJson());
  const tl = s6Timeline(trace, STUDIO_FPS);
  const at = (id: string, offset = 5) => s6State(trace, tl, findBeat(tl, (e) => e.id === id)!.startFrame + offset);
  const level = (state: ReturnType<typeof s6State>, id: string) => state.steps.find((s) => s.id === id)?.level;

  it("starts with every step inert", () => {
    const state = s6State(trace, tl, 0);
    expect(state.steps.every((s) => s.level === "inert")).toBe(true);
    expect(state.runs).toBe(0);
    expect(state.toGreenMs).toBeNull();
  });

  it("goes red, then diagnoses and fixes, with the log recording each", () => {
    expect(level(at("e03"), "red")).toBe("attention");
    expect(at("e03").pr?.status).toBe("CI failed");
    const fixed = at("e05");
    expect(level(fixed, "fixed")).toBe("running");
    expect(fixed.log.map((r) => r.text)).toEqual([
      "Run 1 red · 14 checks",
      "test_retry_budget expects 3 retries and sees 4",
      "stop counting the first attempt as a retry",
    ]);
    expect(fixed.caption?.title).toBe("Fixed");
  });

  it("draws the loop-back edge only once CI runs again", () => {
    expect(at("e05").runs).toBe(1);
    const rerun = at("e06", 20);
    expect(rerun.runs).toBe(2);
    expect(rerun.edges.run).toBeGreaterThan(0);
    expect(rerun.caption?.title).toBe("CI runs again");
  });

  it("ends green after three runs, 46 minutes from the first red", () => {
    const poster = posterFrame(s6SelfCorrection, trace);
    expect(poster).toBe(398);
    const state = s6State(trace, tl, poster);
    expect(level(state, "green")).toBe("done");
    expect(state.steps.filter((s) => s.id !== "green").every((s) => s.level === "done")).toBe(true);
    expect(state.runs).toBe(3);
    expect(state.toGreenMs).toBe(46 * 60_000);
    expect(state.isGreen).toBe(true);
    expect(at("e07").isGreen).toBe(false);
    expect(state.log.at(-1)).toMatchObject({ text: "Run 3 green · 14 checks", detail: "no human step" });
  });
});
