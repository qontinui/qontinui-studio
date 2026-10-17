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
    expect(errors).toContain('events.3.pr: ci.diagnosed while "pr-430" is not red');
    expect(errors.join("\n")).toMatch(/events.4.pr: fix.pushed without a diagnosis/);
  });

  it("refuses a diagnosis of a green PR and a second fix for one diagnosis", () => {
    const base = traceJson();
    const run = (...kinds: string[]) =>
      errorsOf({
        ...base,
        events: kinds.map((kind, n) => ({
          id: `x${n}`,
          at: `2026-09-18T12:${String(n).padStart(2, "0")}:00Z`,
          kind,
          pr: "pr-430",
          ...(kind === "ci.diagnosed" ? { cause: "c" } : kind === "fix.pushed" ? { summary: "s" } : {}),
        })),
      });
    expect(run("ci.failed", "ci.passed", "ci.diagnosed").join("\n")).toMatch(/is not red/);
    expect(run("ci.failed", "ci.diagnosed", "fix.pushed", "fix.pushed").join("\n")).toMatch(/events.3.pr: fix.pushed without/);
    expect(run("ci.failed", "ci.diagnosed", "fix.pushed", "ci.failed", "ci.diagnosed", "fix.pushed", "ci.passed")).toEqual([]);
  });

  it("judges a diagnosis by its own PR only", () => {
    const json = traceJson();
    json.entities!.prs!.push({ id: "pr-b", plan: "plan-retry", repo: "example/sync-service", number: 431, title: "t" });
    json.events.push({ id: "e90", at: "2026-09-18T14:22:00Z", kind: "ci.diagnosed", pr: "pr-b", cause: "c" } as never);
    expect(errorsOf(json)).toContain('events.12.pr: ci.diagnosed while "pr-b" is not red');
  });

  it("needs a closed loop and no operator step", () => {
    const open = traceJson();
    open.events = open.events.filter((e) => e.kind !== "ci.passed" && e.kind !== "pr.landed");
    open.events.push({ id: "e90", at: "2026-09-18T13:59:00Z", kind: "ci.passed", pr: "pr-430" } as never);
    expect(missingRequirements(s6SelfCorrection, parseTrace(open))).toContain(
      "a closed loop: a red run, a fix after it, and a green run after the last fix",
    );
    const human = traceJson();
    human.entities!.sessions = [{ id: "s1", label: "Session" }];
    human.events.push(
      { id: "e90", at: "2026-09-18T14:22:00Z", kind: "question.asked", session: "s1", question: "What now?" } as never,
      { id: "e91", at: "2026-09-18T14:23:00Z", kind: "question.answered", question: "e90", answer: "Do X", from: "operator" } as never,
    );
    expect(missingRequirements(s6SelfCorrection, parseTrace(human))).toContain("no operator step (the scene claims none)");
  });

  it("refuses a fix once the PR is green again", () => {
    const json = traceJson();
    json.events.push({ id: "e90", at: "2026-09-18T15:10:00Z", kind: "fix.pushed", pr: "pr-430", summary: "late" } as never);
    expect(errorsOf(json).join("\n")).toMatch(/events.12.pr: fix.pushed without a diagnosis/);
  });

  it("says what each green was: first try, a re-run, or after a fix", () => {
    const base = traceJson();
    const play = (...kinds: string[]) => {
      const t = parseTrace({
        ...base,
        events: kinds.map((kind, n) => ({
          id: `x${n}`,
          at: `2026-09-18T12:${String(n).padStart(2, "0")}:00Z`,
          kind,
          pr: "pr-430",
          ...(kind === "ci.diagnosed" ? { cause: "c" } : kind === "fix.pushed" ? { summary: "s" } : {}),
        })),
      });
      const ttl = s6Timeline(t, STUDIO_FPS);
      return s6State(t, ttl, ttl.durationInFrames - 1);
    };
    expect(play("ci.started", "ci.passed").log.at(-1)?.detail).toBe("green on the first try");
    expect(play("ci.started", "ci.failed", "ci.started", "ci.passed").log.at(-1)?.detail).toBe("green on a re-run");
    const flaky = play("ci.failed", "ci.diagnosed", "fix.pushed", "ci.started", "ci.passed", "ci.failed", "ci.started", "ci.passed");
    expect(flaky.log.at(-1)?.detail).toBe("green on a re-run");
    expect(flaky.caption?.body).toBe("Every check passes.");
    // No ci.started at all: the run after the fix still draws the loop-back edge.
    expect(play("ci.failed", "ci.diagnosed", "fix.pushed", "ci.passed").edges.run).toBe(1);
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

  it("counts a result with no open run as a run", () => {
    const json = traceJson();
    json.events = json.events.filter((e) => e.id !== "e02");
    const t = parseTrace(json);
    const ttl = s6Timeline(t, STUDIO_FPS);
    const red = s6State(t, ttl, findBeat(ttl, (e) => e.id === "e03")!.startFrame + 5);
    expect(red.log[0]?.text).toBe("Run 1 red · 14 checks");
    expect(red.runs).toBe(1);
  });

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
    expect(at("e05", 20).edges.run).toBe(0);
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
    expect(state.log.at(-1)).toMatchObject({ text: "Run 3 green · 14 checks", detail: "fix pushed · no human step" });
    // The exit edge to green does not redraw at the landing.
    const landed = at("e12", 0);
    expect(landed.edges.green).toBe(1);
  });
});
