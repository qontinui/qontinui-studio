import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { STUDIO_FPS, findBeat, missingRequirements, parseTrace, validateTrace, type SceneTraceInput } from "../../src/api";
import { posterFrame } from "../../src/api/scene-root";
import { s4MergeTrain } from "../../src/scenes/s4-merge-train";
import { s4State, s4Timeline } from "../../src/scenes/s4-merge-train/model";

const here = path.dirname(fileURLToPath(import.meta.url));
const traceJson = (): SceneTraceInput =>
  JSON.parse(readFileSync(path.join(here, "../../traces/illustrative/s4-merge-train.json"), "utf8")) as SceneTraceInput;
const errorsOf = (input: unknown) => {
  const result = validateTrace(input);
  return result.ok ? [] : result.errors;
};

describe("S4 trace fields", () => {
  it("accepts the committed illustrative trace and it drives the scene", () => {
    expect(missingRequirements(s4MergeTrain, parseTrace(traceJson()))).toEqual([]);
  });

  it("refuses a resolution with no conflict before it", () => {
    const json = traceJson();
    const resolved = json.events.find((e) => e.kind === "merge_train.resolved")!;
    resolved.at = "2026-09-16T09:00:30Z";
    expect(errorsOf(json).join("\n")).toMatch(/merge_train.resolved before any merge_train.conflicted for "pr-418"/);
  });

  it("caps the scene at four PRs", () => {
    const json = traceJson();
    json.entities!.prs!.push({ id: "pr-5", plan: "plan-sync", repo: "example/sync-service", number: 5, title: "x" });
    expect(missingRequirements(s4MergeTrain, parseTrace(json))).toContain("at most 4 PRs (the trace has 5)");
  });
});

describe("S4 scene model", () => {
  const trace = parseTrace(traceJson());
  const tl = s4Timeline(trace, STUDIO_FPS);
  const beat = (kind: string, pr: string) => findBeat(tl, (e) => e.kind === kind && "pr" in e && e.pr === pr)!;
  const settled = (b: { startFrame: number }) => s4State(trace, tl, b.startFrame + tl.beatFrames - 1);
  const order = (frame: number) => s4State(trace, tl, frame).cars.map((c) => c.number);

  it("queues the PRs in order, head first", () => {
    const state = settled(beat("merge_train.queued", "pr-421"));
    expect(state.cars.map((c) => [c.number, c.slot, c.status])).toEqual([
      [412, 0, "Queued"],
      [415, 1, "Queued"],
      [418, 2, "Queued"],
      [421, 3, "Queued"],
    ]);
  });

  it("rebases and tests the head, then lands it and the rest move up", () => {
    expect(settled(beat("merge_train.rebased", "pr-412")).cars[0]).toMatchObject({ number: 412, status: "Rebased · testing", level: "testing" });
    const landing = beat("pr.landed", "pr-412");
    expect(s4State(trace, tl, landing.startFrame).cars[0]?.number).toBe(412);
    const after = settled(landing);
    expect(after.cars.map((c) => c.number)).toEqual([415, 418, 421]);
    expect(after.cars.map((c) => c.slot)).toEqual([0, 1, 2]);
    expect(after.main.map((r) => r.text)).toEqual(["#412 feat: retry budget for sync jobs"]);
    expect(after.landed).toBe(1);
  });

  it("moves a conflicted PR out of line so the next one goes first", () => {
    const state = settled(beat("merge_train.conflicted", "pr-418"));
    expect(state.cars.map((c) => [c.number, c.slot, c.aside, c.level])).toEqual([
      [421, 0, 0, "waiting"],
      [418, 1, 1, "attention"],
    ]);
    expect(state.caption?.title).toBe("A conflict");
  });

  it("slides cars between slots rather than jumping", () => {
    const conflict = beat("merge_train.conflicted", "pr-418");
    const mid = s4State(trace, tl, conflict.startFrame + 5).cars.find((c) => c.number === 421)!;
    expect(mid.slot).toBeGreaterThan(0);
    expect(mid.slot).toBeLessThan(1);
  });

  it("lands every PR, the resolved one last, in landing order", () => {
    const end = s4State(trace, tl, tl.durationInFrames - 1);
    expect(end.cars).toEqual([]);
    expect(end.main.map((r) => r.text.split(" ")[0])).toEqual(["#412", "#415", "#421", "#418"]);
    expect(end.landed).toBe(4);
    const resolved = settled(beat("merge_train.resolved", "pr-418"));
    expect(resolved.cars).toMatchObject([{ number: 418, aside: 0, status: "Resolved · queued" }]);
  });

  it("puts the poster on the conflict with the next PR at the head", () => {
    const poster = posterFrame(s4MergeTrain, trace);
    expect(poster).toBe(359);
    expect(order(poster)).toEqual([421, 418]);
    expect(s4State(trace, tl, poster).cars[0]?.status).toBe("Rebased · testing");
  });
});
