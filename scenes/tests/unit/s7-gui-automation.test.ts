import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { STUDIO_FPS, findBeat, missingRequirements, parseTrace, validateTrace, type SceneTraceInput } from "../../src/api";
import { posterFrame } from "../../src/api/scene-root";
import { s7GuiAutomation } from "../../src/scenes/s7-gui-automation";
import { s7State, s7Timeline } from "../../src/scenes/s7-gui-automation/model";

const here = path.dirname(fileURLToPath(import.meta.url));
const traceJson = (): SceneTraceInput =>
  JSON.parse(readFileSync(path.join(here, "../../traces/illustrative/s7-gui-automation.json"), "utf8")) as SceneTraceInput;
const errorsOf = (input: unknown) => {
  const result = validateTrace(input);
  return result.ok ? [] : result.errors;
};
type Mutable = Record<string, unknown>;

describe("S7 trace fields", () => {
  it("accepts the committed illustrative trace and it drives the scene", () => {
    expect(missingRequirements(s7GuiAutomation, parseTrace(traceJson()))).toEqual([]);
  });

  it("refuses a transition between unknown states", () => {
    const json = traceJson();
    json.entities!.transitions![0]!.to = "st-nowhere";
    expect(errorsOf(json)).toContain('entities.transitions.0.to: unknown state "st-nowhere"');
  });

  it("refuses learning a transition before its states", () => {
    const json = traceJson();
    json.events[2]!.at = "2026-09-19T10:00:13Z"; // t-signin found right after Sign in, before Home
    expect(errorsOf(json)).toContain('events.2.transition: "t-signin" is found before its state "st-home"');
  });

  it("refuses finding twice, taking an unlearned transition, and a broken path", () => {
    const twice = traceJson();
    twice.events.push({ id: "e90", at: "2026-09-19T10:02:30Z", kind: "gui.state_found", state: "st-home" } as never);
    expect(errorsOf(twice)).toContain('events.15.state: "st-home" is found more than once');
    const early = traceJson();
    (early.events[12] as Mutable).transition = "t-search";
    early.events[12]!.at = "2026-09-19T10:00:30Z";
    expect(errorsOf(early).join("\n")).toMatch(/"t-search" is taken before it is found/);
    const jump = traceJson();
    (jump.events[13] as Mutable).transition = "t-add";
    expect(errorsOf(jump)).toContain('events.13.transition: "t-add" starts at "st-results", but the traversal is at "st-home"');
  });

  it("caps the scene at six states", () => {
    const json = traceJson();
    json.entities!.states!.push({ id: "st-7", label: "Extra" });
    expect(missingRequirements(s7GuiAutomation, parseTrace(json))).toContain("at most 6 states (the trace has 7)");
  });
});

describe("S7 scene model", () => {
  const trace = parseTrace(traceJson());
  const tl = s7Timeline(trace, STUDIO_FPS);
  const at = (id: string, offset = 5) => s7State(trace, tl, findBeat(tl, (e) => e.id === id)!.startFrame + offset);

  it("explores first: states in discovery slots, a fresh one highlighted", () => {
    const state = at("e04");
    expect(state.phase).toBe("exploring");
    expect(state.states.map((s) => [s.label, s.slot])).toEqual([
      ["Sign in", 0],
      ["Home", 1],
      ["Search", 2],
    ]);
    expect(state.states.at(-1)?.level).toBe("testing");
    expect(state.caption?.title).toBe("A state is found");
  });

  it("learns transitions as edges, undrawn until taken", () => {
    const state = at("e12");
    expect(state.actionsLearned).toBe(6);
    expect(state.edges.every((e) => e.level === "inert" && e.progress === 0)).toBe(true);
    expect(state.model.at(-1)).toMatchObject({ text: "Home → Cart", detail: "action: open Cart" });
  });

  it("traverses the learned graph to the goal", () => {
    const first = at("e13");
    expect(first.phase).toBe("traversing");
    expect(first.states.find((s) => s.label === "Home")?.level).toBe("running");
    expect(first.states.find((s) => s.label === "Sign in")?.level).toBe("done");
    const poster = posterFrame(s7GuiAutomation, trace);
    expect(poster).toBe(440);
    const end = s7State(trace, tl, poster);
    expect(end.steps).toBe(3);
    expect(end.states.find((s) => s.label === "Checkout")?.level).toBe("running");
    expect(end.edges.filter((e) => e.progress === 1).map((e) => e.id).sort()).toEqual(["t-cart", "t-checkout", "t-signin"]);
    expect(end.edges.find((e) => e.id === "t-checkout")?.level).toBe("running");
    expect(end.edges.find((e) => e.id === "t-search")?.level).toBe("inert");
    expect(end.caption?.title).toBe("Traversed by an action");
  });
});
