import { describe, expect, it } from "vitest";

import {
  activeBeatIndex,
  buildTimeline,
  findBeat,
  formatDate,
  formatElapsed,
  missingRequirements,
  modeLine,
  parseTrace,
  realTimeAt,
  sortEvents,
} from "../../src/api";
import { STAGE_KINDS, s1State } from "../../src/scenes/s1-plan-lifecycle/model";
import { s1PlanLifecycle } from "../../src/scenes/s1-plan-lifecycle";
import { clone, replayTraceJson, s1TraceJson } from "./fixtures";

const opts = { fps: 30, introMs: 1000, beatMs: 500, outroMs: 2000 };

describe("buildTimeline", () => {
  it("places one beat per event at fixed intervals after the intro", () => {
    const trace = parseTrace(replayTraceJson());
    const tl = buildTimeline(trace, opts);
    expect(tl.beats.map((b) => b.startFrame)).toEqual([30, 45]);
    expect(tl.beatFrames).toBe(15);
    expect(tl.durationInFrames).toBe(45 + 15 + 60);
  });

  it("orders by timestamp, then by trace order, regardless of input order", () => {
    const json = clone(s1TraceJson());
    json.events.reverse();
    const reversed = parseTrace(json);
    const original = parseTrace(s1TraceJson());
    const ids = (t: typeof original) => sortEvents(t.events).map((e) => e.id);
    // e05/e06 share a timestamp: reversing the input flips their tie-break.
    expect(ids(original)).toEqual(["e01", "e02", "e03", "e04", "e05", "e06", "e07", "e08", "e09", "e10", "e11", "e12", "e13", "e14", "e15"]);
    expect(ids(reversed).filter((id) => id !== "e05" && id !== "e06")).toEqual(
      ids(original).filter((id) => id !== "e05" && id !== "e06"),
    );
  });

  it("filters beats by kind", () => {
    const trace = parseTrace(s1TraceJson());
    const tl = buildTimeline(trace, { ...opts, kinds: STAGE_KINDS });
    expect(tl.beats.some((b) => b.event.kind.startsWith("session."))).toBe(false);
    expect(tl.beats).toHaveLength(11);
  });

  it("is deterministic", () => {
    const trace = parseTrace(s1TraceJson());
    expect(buildTimeline(trace, opts)).toEqual(buildTimeline(trace, opts));
  });

  it("activeBeatIndex, findBeat and realTimeAt", () => {
    const trace = parseTrace(replayTraceJson());
    const tl = buildTimeline(trace, opts);
    expect(activeBeatIndex(tl, 0)).toBe(-1);
    expect(activeBeatIndex(tl, 30)).toBe(0);
    expect(activeBeatIndex(tl, 44)).toBe(0);
    expect(activeBeatIndex(tl, 45)).toBe(1);
    expect(findBeat(tl, (e) => e.kind === "plan.vetted")?.startFrame).toBe(45);
    const t0 = Date.parse("2026-09-02T10:00:00Z");
    const t1 = Date.parse("2026-09-03T10:00:00Z");
    expect(realTimeAt(tl, 0)).toBe(t0);
    expect(realTimeAt(tl, 37.5)).toBe((t0 + t1) / 2);
    expect(realTimeAt(tl, 500)).toBe(t1);
  });
});

describe("S1 scene", () => {
  const trace = parseTrace(s1TraceJson());
  const tl = s1PlanLifecycle.timeline(trace, 30);

  it("runs 10-15 s at 30 fps", () => {
    expect(tl.durationInFrames / 30).toBeGreaterThanOrEqual(10);
    expect(tl.durationInFrames / 30).toBeLessThanOrEqual(15);
  });

  it("declares requirements the illustrative trace meets", () => {
    expect(missingRequirements(s1PlanLifecycle, trace)).toEqual([]);
    expect(missingRequirements(s1PlanLifecycle, parseTrace(replayTraceJson()))).toContain('event kind "pr.opened"');
  });

  it("starts with every stage inert and ends with every stage done", () => {
    expect(s1State(trace, tl, 0).stages.every((s) => s.level === "inert")).toBe(true);
    expect(s1State(trace, tl, tl.durationInFrames - 1).stages.every((s) => s.level === "done")).toBe(true);
  });

  it("colours a failed CI as 'attention' and a passed one as 'done'", () => {
    const failed = findBeat(tl, (e) => e.kind === "ci.failed");
    const passed = findBeat(tl, (e) => e.kind === "ci.passed");
    if (!failed || !passed) throw new Error("fixture lacks CI beats");
    const atFail = s1State(trace, tl, failed.startFrame);
    expect(atFail.stages[4]?.level).toBe("attention");
    expect(atFail.pr?.status).toBe("CI failed");
    expect(atFail.gate?.open).toBe(0);
    const afterPass = s1State(trace, tl, passed.startFrame + 30);
    expect(afterPass.stages[4]?.level).toBe("done");
    expect(afterPass.gate?.open).toBe(1);
  });

  it("a landed PR is done while the plan is still landing", () => {
    const landed = findBeat(tl, (e) => e.kind === "pr.landed");
    if (!landed) throw new Error("fixture lacks pr.landed");
    const state = s1State(trace, tl, landed.startFrame);
    expect(state.pr?.level).toBe("done");
    expect(state.stages[6]?.level).toBe("landing");
  });

  it("shows no PR card before the PR opens", () => {
    const opened = findBeat(tl, (e) => e.kind === "pr.opened");
    if (!opened) throw new Error("fixture lacks pr.opened");
    expect(s1State(trace, tl, opened.startFrame - 1).pr).toBeNull();
    expect(s1State(trace, tl, opened.startFrame).pr?.number).toBe(412);
  });
});

describe("formatting", () => {
  it("formats dates in UTC and elapsed time compactly", () => {
    expect(formatDate("2026-09-14T23:30:00-05:00")).toBe("2026-09-15");
    expect(formatElapsed(45 * 60_000)).toBe("45m");
    expect(formatElapsed((3 * 60 + 20) * 60_000)).toBe("3h 20m");
    expect(formatElapsed((2 * 24 + 4) * 3_600_000)).toBe("2d 4h");
  });

  it("the mode line states illustrative, or replay with range and source (plan D2)", () => {
    expect(modeLine(parseTrace(s1TraceJson()).meta)).toEqual({
      badge: "ILLUSTRATIVE",
      detail: "Synthetic trace · not a real build",
    });
    expect(modeLine(parseTrace(replayTraceJson()).meta)).toEqual({
      badge: "REPLAY",
      detail: "2026-09-01 – 2026-09-07 · source: qontinui/qontinui-design-tokens",
    });
  });
});
