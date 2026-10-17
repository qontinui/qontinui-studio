import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { STUDIO_FPS, findBeat, missingRequirements, parseTrace, validateTrace, type SceneTraceInput } from "../../src/api";
import { posterFrame } from "../../src/api/scene-root";
import { s3GatesContinuations } from "../../src/scenes/s3-gates-continuations";
import { s3State, s3Timeline } from "../../src/scenes/s3-gates-continuations/model";

const here = path.dirname(fileURLToPath(import.meta.url));
const traceJson = (): SceneTraceInput =>
  JSON.parse(readFileSync(path.join(here, "../../traces/illustrative/s3-gates-continuations.json"), "utf8")) as SceneTraceInput;
const errorsOf = (input: unknown) => {
  const result = validateTrace(input);
  return result.ok ? [] : result.errors;
};
type Mutable = Record<string, unknown>;

describe("S3 trace fields", () => {
  it("accepts the committed illustrative trace and it drives the scene", () => {
    expect(missingRequirements(s3GatesContinuations, parseTrace(traceJson()))).toEqual([]);
  });

  it("refuses an unknown gate and an unknown continuation", () => {
    const json = traceJson();
    (json.events[1] as Mutable).gate = "g-nowhere";
    (json.events[5] as Mutable).continues = "g-elsewhere";
    const errors = errorsOf(json);
    expect(errors).toContain('events.1.gate: unknown gate "g-nowhere"');
    expect(errors).toContain('events.5.continues: unknown continues "g-elsewhere"');
  });

  it("refuses an observation or clearing before the gate is registered", () => {
    const json = traceJson();
    json.events[3]!.at = "2026-09-15T10:00:00Z";
    expect(errorsOf(json)).toContain('events.3.gate: gate.observed before gate.registered for "g-upstream"');
  });

  it("refuses a continuation that starts before its gate cleared", () => {
    const json = traceJson();
    json.events[5]!.at = "2026-09-15T16:00:00Z";
    expect(errorsOf(json)).toContain('events.5.continues: a continuation starts before gate.cleared for "g-upstream"');
  });

  it("refuses a gate registered twice or cleared twice", () => {
    const json = traceJson();
    json.events.push({ id: "e98", at: "2026-09-15T20:00:00Z", kind: "gate.cleared", gate: "g-upstream" } as never);
    json.events.push({ id: "e99", at: "2026-09-15T20:00:00Z", kind: "gate.registered", gate: "g-upstream", session: "s-close" } as never);
    const errors = errorsOf(json);
    expect(errors).toContain('events.12.gate: gate.cleared occurs more than once for "g-upstream"');
    expect(errors).toContain('events.13.gate: gate "g-upstream" is registered more than once');
  });

  it("needs a continuation and at most three sessions", () => {
    const json = traceJson();
    for (const e of json.events) delete (e as Mutable).continues;
    json.entities!.sessions!.push({ id: "s-4", label: "Session 4" });
    const problems = missingRequirements(s3GatesContinuations, parseTrace(json));
    expect(problems).toContain("at least one continuation (a session.started with `continues`)");
    expect(problems).toContain("at most three sessions (the trace has 4)");
  });
});

describe("S3 scene model", () => {
  const trace = parseTrace(traceJson());
  const tl = s3Timeline(trace, STUDIO_FPS);
  const beat = (id: string) => findBeat(tl, (e) => e.id === id)!;

  it("shows the working session and a waiting placeholder before any gate", () => {
    const state = s3State(trace, tl, beat("e01").startFrame + 5);
    expect(state.gate).toBeNull();
    expect(state.registrant).toMatchObject({ label: "Session 1 · implementing", level: "running" });
    expect(state.continuation).toMatchObject({ label: "Next session", level: "inert" });
    expect(state.running).toBe(1);
  });

  it("closes the gate on registration, with the session still running", () => {
    const state = s3State(trace, tl, beat("e02").startFrame + 5);
    expect(state.gate).toMatchObject({ label: "the upstream PR example/api#88 has landed", level: "waiting", open: 0 });
    expect(state.registrant?.sublabel).toBe("registered a gate");
    expect(state.caption?.title).toBe("Blocked, so it registers a gate");
  });

  it("runs nothing while the condition is false", () => {
    // Halfway between the session ending and the condition being observed.
    const frame = Math.round((beat("e03").startFrame + beat("e04").startFrame) / 2);
    const state = s3State(trace, tl, frame);
    expect(state.running).toBe(0);
    expect(state.registrant).toMatchObject({ level: "done", sublabel: "ended · nothing polls" });
    expect(state.waitedMs).toBeGreaterThan(60 * 60_000);
    expect(state.gates[0]?.detail).toBe("waiting since 2026-09-15 11:40 UTC · registered by Session 1 · implementing");
  });

  it("observes, clears and opens the gate, then the continuation starts", () => {
    expect(s3State(trace, tl, beat("e04").startFrame).gate?.level).toBe("testing");
    const cleared = s3State(trace, tl, beat("e05").startFrame + tl.beatFrames - 1);
    expect(cleared.gate).toMatchObject({ level: "done", open: 1 });
    expect(cleared.edgeOut).toBe(1);
    const next = s3State(trace, tl, beat("e06").startFrame + 5);
    expect(next.continuation).toMatchObject({ label: "Session 2 · continuation", level: "running" });
    expect(next.caption?.title).toBe("A new session picks up the work");
    expect(next.waitedMs).toBe(Date.parse("2026-09-15T16:05:20Z") - Date.parse("2026-09-15T11:40:00Z"));
  });

  it("moves the focus to the second gate and keeps both in the panel", () => {
    const state = s3State(trace, tl, beat("e07").startFrame + 5);
    expect(state.gate?.label).toBe("PR #415 has landed on main");
    expect(state.registrant?.label).toBe("Session 2 · continuation");
    expect(state.continuation.level).toBe("inert");
    expect(state.gates.map((g) => g.level)).toEqual(["done", "waiting"]);
    expect(state.gates[0]?.detail).toBe("cleared · continued by Session 2 · continuation");
  });

  it("puts the poster on the first continuation", () => {
    const poster = posterFrame(s3GatesContinuations, trace);
    expect(poster).toBe(251);
    expect(tl.beats.find((b) => b.startFrame === 252)?.event.id).toBe("e07");
    expect(s3State(trace, tl, poster).continuation.label).toBe("Session 2 · continuation");
  });
});
