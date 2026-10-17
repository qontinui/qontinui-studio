import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { STUDIO_FPS, findBeat, missingRequirements, parseTrace, validateTrace, type SceneTraceInput } from "../../src/api";
import { posterFrame } from "../../src/api/scene-root";
import { s5Fleet } from "../../src/scenes/s5-fleet";
import { s5State, s5Timeline } from "../../src/scenes/s5-fleet/model";

const here = path.dirname(fileURLToPath(import.meta.url));
const traceJson = (): SceneTraceInput =>
  JSON.parse(readFileSync(path.join(here, "../../traces/illustrative/s5-fleet.json"), "utf8")) as SceneTraceInput;
const errorsOf = (input: unknown) => {
  const result = validateTrace(input);
  return result.ok ? [] : result.errors;
};
type Mutable = Record<string, unknown>;

describe("S5 trace fields", () => {
  it("accepts the committed illustrative trace and it drives the scene", () => {
    expect(missingRequirements(s5Fleet, parseTrace(traceJson()))).toEqual([]);
  });

  it("refuses an unknown machine", () => {
    const json = traceJson();
    json.entities!.sessions![0]!.machine = "m-nowhere";
    expect(errorsOf(json)).toContain('entities.sessions.0.machine: unknown machine "m-nowhere"');
  });

  it("refuses a contention against a session that does not hold the claim", () => {
    const json = traceJson();
    (json.events[7] as Mutable).holder = "s3";
    expect(errorsOf(json)).toContain('events.7.holder: "s3" does not hold "sync/worker.ts" at that point');
    (json.events[7] as Mutable).holder = "s2";
    expect(errorsOf(json)).toContain("events.7.holder: a session cannot contend with itself");
  });

  it("refuses a claim already held by another session, and a release without holding", () => {
    const taken = traceJson();
    (taken.events[9] as Mutable).resource = "sync/worker.ts";
    expect(errorsOf(taken)).toContain('events.9.resource: "sync/worker.ts" is already held by "s1"');
    const stray = traceJson();
    (stray.events[16] as Mutable).session = "s3";
    expect(errorsOf(stray)).toContain('events.16.resource: "s3" releases "sync/worker.ts" without holding it');
  });

  it("orders claims by time, then by position, whatever the array order", () => {
    // The release listed first but later in time is accepted.
    const json = traceJson();
    const release = json.events.splice(16, 1)[0]!;
    json.events.unshift(release);
    expect(errorsOf(json)).toEqual([]);
    // An acquire at the release's instant: refused before it, accepted after it.
    const before = traceJson();
    const grab = { id: "e98", at: before.events[16]!.at, kind: "claim.acquired", session: "s2", resource: "sync/worker.ts" };
    before.events.splice(16, 0, grab as never);
    expect(errorsOf(before)).toContain('events.16.resource: "sync/worker.ts" is already held by "s1"');
    const after = traceJson();
    after.events.splice(17, 0, grab as never);
    expect(errorsOf(after)).toEqual([]);
  });

  it("releases a session's claims when it ends, and refuses acting outside its life", () => {
    const json = traceJson();
    json.events = json.events.filter((e) => e.id !== "e17");
    json.events.push({ id: "e98", at: "2026-09-17T10:00:00Z", kind: "claim.acquired", session: "s2", resource: "sync/worker.ts" } as never);
    expect(errorsOf(json)).toEqual([]);
    const late = traceJson();
    late.events.push({ id: "e99", at: "2026-09-17T10:00:00Z", kind: "intent.declared", session: "s1", intent: "one more thing" } as never);
    expect(errorsOf(late)).toContain('events.18.session: "s1" acts after it ended (intent.declared)');
    const early = traceJson();
    early.events[10]!.at = "2026-09-17T09:13:30Z";
    expect(errorsOf(early)).toContain('events.11.session: "s3" acts before it started (worktree.allocated)');
  });

  it("refuses a second start or end, and an ended session's acquire takes nothing", () => {
    const json = traceJson();
    json.events.push({ id: "e97", at: "2026-09-17T09:58:00Z", kind: "session.started", session: "s1" } as never);
    json.events.push({ id: "e98", at: "2026-09-17T09:59:00Z", kind: "session.ended", session: "s1" } as never);
    const errors = errorsOf(json);
    expect(errors).toContain('events.18.session: "s1" starts more than once');
    expect(errors).toContain('events.19.session: "s1" ends more than once');
    const ghost = traceJson();
    ghost.events.push({ id: "e97", at: "2026-09-17T09:57:00Z", kind: "claim.acquired", session: "s1", resource: "x.ts" } as never);
    ghost.events.push({ id: "e98", at: "2026-09-17T09:58:00Z", kind: "claim.acquired", session: "s2", resource: "x.ts" } as never);
    const ghostErrors = errorsOf(ghost);
    expect(ghostErrors).toContain('events.18.session: "s1" acts after it ended (claim.acquired)');
    expect(ghostErrors.join("\n")).not.toMatch(/already held by "s1"/);
  });

  it("refuses a holder re-acquiring its own claim", () => {
    const json = traceJson();
    json.events.push({ id: "e99", at: "2026-09-17T09:04:00Z", kind: "claim.acquired", session: "s1", resource: "sync/worker.ts" } as never);
    expect(errorsOf(json)).toContain('events.18.resource: "s1" already holds "sync/worker.ts"');
  });

  it("lets a released claim be taken again", () => {
    const json = traceJson();
    json.events.push({ id: "e99", at: "2026-09-17T10:00:00Z", kind: "claim.acquired", session: "s2", resource: "sync/worker.ts" } as never);
    expect(errorsOf(json)).toEqual([]);
  });

  it("needs every acting session to have started", () => {
    const json = traceJson();
    json.events = json.events.filter((e) => e.id !== "e11");
    expect(missingRequirements(s5Fleet, parseTrace(json))).toContain("a session.started for every session that acts (not: s3)");
  });

  it("checks machines and sessions per machine", () => {
    const json = traceJson();
    json.entities!.sessions!.push({ id: "s5", label: "Session 5", machine: "m-a" }, { id: "s6", label: "Session 6" });
    const problems = missingRequirements(s5Fleet, parseTrace(json));
    expect(problems).toContain("every session on a machine (not: s6)");
    expect(problems).toContain("at most 2 sessions per machine (m-a has 3)");
  });
});

describe("S5 scene model", () => {
  const trace = parseTrace(traceJson());
  const tl = s5Timeline(trace, STUDIO_FPS);
  const beat = (id: string) => findBeat(tl, (e) => e.id === id)!;
  const at = (id: string) => s5State(trace, tl, beat(id).startFrame + 5);

  it("puts sessions on their machines as they start, each with its worktree", () => {
    expect(s5State(trace, tl, 0).machines.every((m) => m.sessions.length === 0)).toBe(true);
    const state = at("e02");
    expect(state.machines[0]?.sessions).toMatchObject([{ text: "Session 1 · sync-service", detail: "in its own worktree" }]);
    expect(state.caption?.title).toBe("Its own worktree");
  });

  it("logs intent and claims, and counts an avoided collision", () => {
    const state = at("e08");
    expect(state.machines[1]?.sessions[0]).toMatchObject({ level: "waiting", detail: "sync/worker.ts taken · other work" });
    expect(state.log.at(-1)).toMatchObject({ text: "sync/worker.ts", detail: "Session 2 sees it held by Session 1 · no collision" });
    expect(state.avoided).toBe(1);
    expect(state.caption?.title).toBe("Already claimed");
  });

  it("runs four sessions at the poster and one fewer once Session 1 ends", () => {
    const poster = posterFrame(s5Fleet, trace);
    expect(poster).toBe(467);
    const state = s5State(trace, tl, poster);
    expect(state.running).toBe(4);
    expect(state.machines.map((m) => m.sessions.length)).toEqual([2, 1, 1]);
    const end = s5State(trace, tl, tl.durationInFrames - 1);
    expect(end.running).toBe(3);
    expect(end.machines[0]?.sessions[0]).toMatchObject({
      text: "Session 1",
      level: "done",
      detail: "done · claims released",
    });
  });

  it("shows everything a session holds, not only its latest event", () => {
    const json = traceJson();
    json.events.push({ id: "e98", at: "2026-09-17T09:19:00Z", kind: "claim.acquired", session: "s4", resource: "sync/cli.ts" } as never);
    json.events.push({ id: "e99", at: "2026-09-17T09:20:00Z", kind: "claim.released", session: "s4", resource: "sync/config.ts" } as never);
    const more = parseTrace(json);
    const mtl = s5Timeline(more, STUDIO_FPS);
    const s4 = (id: string) => s5State(more, mtl, findBeat(mtl, (e) => e.id === id)!.startFrame + 5).machines[0]?.sessions[1];
    expect(s4("e98")?.detail).toBe("holds sync/config.ts, sync/cli.ts");
    expect(s4("e99")?.detail).toBe("holds sync/cli.ts");
  });

  it("keeps the log to its capacity", () => {
    expect(s5State(trace, tl, tl.durationInFrames - 1).log.length).toBeLessThanOrEqual(5);
  });
});
