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
    expect(errorsOf(json).join("\n")).toMatch(/merge_train.resolved without an unresolved merge_train.conflicted for "pr-418"/);
  });

  it("refuses a second resolution, a rebase or landing while conflicted, and landing twice", () => {
    const twice = traceJson();
    twice.events.push({ id: "e90", at: "2026-09-16T10:21:00Z", kind: "merge_train.resolved", pr: "pr-418" } as never);
    expect(errorsOf(twice).join("\n")).toMatch(/events.18.pr: merge_train.resolved without an unresolved/);
    const early = traceJson();
    early.events.push({ id: "e91", at: "2026-09-16T10:04:00Z", kind: "merge_train.rebased", pr: "pr-418" } as never);
    expect(errorsOf(early)).toContain('events.18.pr: merge_train.rebased while "pr-418" has an unresolved conflict');
    const again = traceJson();
    again.events.push({ id: "e92", at: "2026-09-16T11:30:00Z", kind: "pr.landed", pr: "pr-412" } as never);
    expect(errorsOf(again)).toContain('events.18.pr: "pr-412" lands more than once');
  });

  it("keeps a red PR out of the train until it is green again or re-queued", () => {
    const json = traceJson();
    json.events.splice(5, 1, { id: "e06", at: "2026-09-16T09:34:00Z", kind: "ci.failed", pr: "pr-412" } as never);
    expect(errorsOf(json)).toContain('events.6.pr: pr.landed while "pr-412" is red (it needs ci.passed or merge_train.queued first)');
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
    // The landed car leaves first, drawn last so nothing slides over it.
    const leaving = s4State(trace, tl, landing.startFrame + 4);
    expect(leaving.cars.at(-1)).toMatchObject({ number: 412, status: "Landed" });
    expect(leaving.cars.at(-1)!.exit).toBeGreaterThan(0);
    expect(leaving.cars.find((c) => c.number === 415)?.slot).toBe(1);
    expect(leaving.main.at(-1)?.text).toBe("#412 feat: retry budget for sync jobs");
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
    expect(resolved.cars.map((c) => [c.number, c.slot, c.aside])).toEqual([
      [421, 0, 0],
      [418, 1, 0],
    ]);
    expect(resolved.cars[1]?.status).toBe("Resolved · queued");
  });

  it("puts a PR that goes green again back in line, at the back", () => {
    const json = traceJson();
    json.events.splice(5, 0, { id: "e90", at: "2026-09-16T09:20:00Z", kind: "ci.failed", pr: "pr-412" } as never);
    const red = parseTrace(json);
    const rtl = s4Timeline(red, STUDIO_FPS);
    const failed = findBeat(rtl, (e) => e.id === "e90")!;
    const outOfLine = s4State(red, rtl, failed.startFrame + rtl.beatFrames - 1).cars.find((c) => c.number === 412)!;
    expect(outOfLine).toMatchObject({ aside: 1, level: "attention" });
    const passed = findBeat(rtl, (e) => e.kind === "ci.passed" && "pr" in e && e.pr === "pr-412")!;
    const back = s4State(red, rtl, passed.startFrame + rtl.beatFrames - 1).cars;
    expect(back.map((c) => [c.number, c.aside])).toEqual([
      [415, 0],
      [418, 0],
      [421, 0],
      [412, 0],
    ]);
  });

  it("puts the poster on the conflict with the next PR at the head", () => {
    const poster = posterFrame(s4MergeTrain, trace);
    expect(poster).toBe(359);
    expect(order(poster)).toEqual([421, 418]);
    expect(s4State(trace, tl, poster).cars[0]?.status).toBe("Rebased · testing");
  });
});
