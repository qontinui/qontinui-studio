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
    expect(errors).toContain('events.5.continues: unknown gate "g-elsewhere"');
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

  it("orders same-instant events by position: a continuation listed before its clearing is refused", () => {
    const json = traceJson();
    json.events[5]!.at = json.events[4]!.at;
    expect(errorsOf(json)).toEqual([]);
    const swapped = traceJson();
    swapped.events[5]!.at = swapped.events[4]!.at;
    const [cleared, started] = [swapped.events[4]!, swapped.events[5]!];
    swapped.events[4] = started;
    swapped.events[5] = cleared;
    expect(errorsOf(swapped)).toContain('events.4.continues: a continuation starts before gate.cleared for "g-upstream"');
  });

  it("says so when the earlier event never happens at all", () => {
    const json = traceJson();
    json.events = json.events.filter((e) => e.id !== "e02" && e.id !== "e10");
    const errors = errorsOf(json);
    expect(errors).toContain('events.2.gate: gate.observed for a gate that is never registered: "g-upstream"');
    expect(errors.join("\n")).toMatch(/a continuation of a gate that is never cleared: "g-train"/);
  });

  it("refuses a clearing before the observation, and a registration after the session ended", () => {
    const json = traceJson();
    json.events[4]!.at = "2026-09-15T16:04:00Z";
    (json.events[1] as Mutable).session = "s-close";
    json.events.push({ id: "e90", at: "2026-09-15T10:00:00Z", kind: "session.ended", session: "s-close" } as never);
    const errors = errorsOf(json);
    expect(errors).toContain('events.4.gate: gate.cleared before gate.observed for "g-upstream"');
    expect(errors).toContain('events.1.session: "s-close" registers a gate after it ended');
  });

  it("refuses a gate registered twice or cleared twice", () => {
    const json = traceJson();
    json.events.push({ id: "e97", at: "2026-09-15T20:00:00Z", kind: "gate.observed", gate: "g-upstream" } as never);
    json.events.push({ id: "e98", at: "2026-09-15T20:00:00Z", kind: "gate.cleared", gate: "g-upstream" } as never);
    json.events.push({ id: "e99", at: "2026-09-15T20:00:00Z", kind: "gate.registered", gate: "g-upstream", session: "s-close" } as never);
    const errors = errorsOf(json);
    expect(errors).toContain('events.12.gate: gate.observed occurs more than once for "g-upstream"');
    expect(errors).toContain('events.13.gate: gate.cleared occurs more than once for "g-upstream"');
    expect(errors).toContain('events.14.gate: gate "g-upstream" is registered more than once');
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

  it("shows nothing running during the intro", () => {
    const state = s3State(trace, tl, 0);
    expect(state.running).toBe(0);
    expect(state.registrant).toBeNull();
    expect(state.continuation.appear).toBe(0);
    expect(state.lanes.every((l) => l.level === "inert")).toBe(true);
  });

  it("colours the ticks: registration waiting, observation testing, clearing and session end done", () => {
    const state = s3State(trace, tl, tl.durationInFrames - 1);
    expect(state.ticks.map((t) => t.level).slice(0, 6)).toEqual(["running", "waiting", "done", "testing", "done", "running"]);
  });

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

  it("follows the gate the caption is about when two gates are open at once", () => {
    const overlap = parseTrace({
      ...traceJson(),
      events: [
        { id: "a1", at: "2026-09-15T09:00:00Z", kind: "session.started", session: "s-impl" },
        { id: "a2", at: "2026-09-15T09:10:00Z", kind: "gate.registered", gate: "g-upstream", session: "s-impl" },
        { id: "a3", at: "2026-09-15T09:11:00Z", kind: "session.ended", session: "s-impl" },
        { id: "b1", at: "2026-09-15T09:20:00Z", kind: "session.started", session: "s-cont" },
        { id: "b2", at: "2026-09-15T09:30:00Z", kind: "gate.registered", gate: "g-train", session: "s-cont" },
        { id: "b3", at: "2026-09-15T09:31:00Z", kind: "session.ended", session: "s-cont" },
        { id: "c1", at: "2026-09-15T12:00:00Z", kind: "gate.observed", gate: "g-upstream" },
        { id: "c2", at: "2026-09-15T12:00:10Z", kind: "gate.cleared", gate: "g-upstream" },
        { id: "c3", at: "2026-09-15T12:01:00Z", kind: "session.started", session: "s-close", continues: "g-upstream" },
      ],
    });
    const otl = s3Timeline(overlap, STUDIO_FPS);
    const at = (id: string) => s3State(overlap, otl, findBeat(otl, (e) => e.id === id)!.startFrame + 5);
    expect(at("b2").gate?.label).toBe("PR #415 has landed on main");
    expect(at("c1").gate).toMatchObject({ label: "the upstream PR example/api#88 has landed", level: "testing" });
    expect(at("c3").continuation).toMatchObject({ label: "Session 3 · continuation", level: "running" });
    expect(at("c3").gates.map((g) => g.level)).toEqual(["done", "waiting"]);
  });

  it("keeps a gate that never clears waiting, with its wait growing", () => {
    const base = traceJson();
    const open = parseTrace({
      ...base,
      entities: { ...base.entities, sessions: base.entities!.sessions!.slice(0, 1) },
      events: base.events.slice(0, 3),
    });
    const otl = s3Timeline(open, STUDIO_FPS);
    const end = s3State(open, otl, otl.durationInFrames - 1);
    expect(end.gate).toMatchObject({ level: "waiting", open: 0 });
    expect(end.continuation.label).toBe("Next session");
    expect(end.waitedMs).toBe(60_000);
    expect(end.running).toBe(0);
  });

  it("counts a session with no start event as running from the beginning", () => {
    const base = traceJson();
    const noStart = parseTrace({ ...base, events: base.events.filter((e) => e.id !== "e01") });
    const ntl = s3Timeline(noStart, STUDIO_FPS);
    const state = s3State(noStart, ntl, findBeat(ntl, (e) => e.id === "e02")!.startFrame + 5);
    expect(state.running).toBe(1);
    expect(state.registrant?.level).toBe("running");
  });
});
