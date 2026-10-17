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

  it("caps the scene at six states found (declared but unfound states do not count)", () => {
    const json = traceJson();
    json.entities!.states!.push({ id: "st-7", label: "Extra" });
    expect(missingRequirements(s7GuiAutomation, parseTrace(json))).toEqual([]);
    json.events.push({ id: "e90", at: "2026-09-19T10:02:40Z", kind: "gui.state_found", state: "st-7" } as never);
    expect(missingRequirements(s7GuiAutomation, parseTrace(json))).toContain("at most 6 states found (the trace finds 7)");
  });

  it("refuses graphs the layout would draw wrongly", () => {
    const withEdge = (id: string, from: string, to: string) => {
      const json = traceJson();
      json.entities!.transitions!.push({ id, from, to, action: "x" });
      json.events.push({ id: "e90", at: "2026-09-19T10:02:40Z", kind: "gui.transition_found", transition: id } as never);
      return missingRequirements(s7GuiAutomation, parseTrace(json));
    };
    expect(withEdge("t-self", "st-home", "st-home")).toContain("no self-loop (t-self)");
    expect(withEdge("t-back", "st-home", "st-login")).toContain("one transition per pair of states (t-back repeats st-home/st-login)");
    // Sign in (slot 0) → Search (slot 2) would run behind Home (slot 1).
    expect(withEdge("t-skip", "st-login", "st-search").join("\n")).toMatch(/t-skip would run through "st-home" at 16:9/);
  });

  it("refuses two edges that cross as an X (their labels would land on each other)", () => {
    const json = traceJson();
    // Home (slot 1) → Results (slot 3) crosses Search (slot 2) → Cart (slot 4).
    json.entities!.transitions!.push(
      { id: "t-x1", from: "st-home", to: "st-results", action: "open Settings" },
      { id: "t-x2", from: "st-search", to: "st-cart", action: "press the Back key" },
    );
    json.events.push(
      { id: "e90", at: "2026-09-19T10:02:40Z", kind: "gui.transition_found", transition: "t-x1" } as never,
      { id: "e91", at: "2026-09-19T10:02:41Z", kind: "gui.transition_found", transition: "t-x2" } as never,
    );
    expect(missingRequirements(s7GuiAutomation, parseTrace(json)).join("\n")).toMatch(/the labels of t-x1 and t-x2 overlap/);
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

  it("learns transitions as edges, unlit until taken, the newest one highlighted", () => {
    const state = at("e12");
    expect(state.actionsLearned).toBe(6);
    expect(state.edges.every((e) => e.progress === 0)).toBe(true);
    expect(state.edges.map((e) => e.level)).toEqual(["inert", "inert", "inert", "inert", "inert", "testing"]);
    expect(state.states.every((st) => st.level === "inert")).toBe(true);
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

  it("follows the latest take of an edge taken twice, and allows revisiting a state", () => {
    const json = traceJson();
    json.entities!.transitions!.push({ id: "t-home", from: "st-results", to: "st-home", action: "click Home" });
    json.events = json.events.filter((e) => e.kind !== "gui.transition_taken");
    json.events.push({ id: "e90", at: "2026-09-19T10:02:30Z", kind: "gui.transition_found", transition: "t-home" } as never);
    ["t-signin", "t-search", "t-submit", "t-home", "t-search"].forEach((transition, n) =>
      json.events.push({ id: `e9${n + 1}`, at: `2026-09-19T10:04:0${n}Z`, kind: "gui.transition_taken", transition } as never),
    );
    expect(missingRequirements(s7GuiAutomation, parseTrace(json))).toEqual([]);
    const again = parseTrace(json);
    const atl = s7Timeline(again, STUDIO_FPS);
    const end = s7State(again, atl, atl.durationInFrames - 1);
    expect(end.edges.find((e) => e.id === "t-search")?.level).toBe("running");
    expect(end.edges.find((e) => e.id === "t-home")?.level).toBe("done");
    expect(end.states.find((st) => st.label === "Search")?.level).toBe("running");
    expect(end.states.find((st) => st.label === "Home")?.level).toBe("done");
    expect(end.steps).toBe(5);
  });

  // Unreachable for a trace the scene accepts (it requires a taken
  // transition), but posterFrame must still return a frame for such a trace.
  it("puts the poster on the last frame when nothing is taken", () => {
    const json = traceJson();
    json.events = json.events.filter((e) => e.kind !== "gui.transition_taken");
    const t = parseTrace(json);
    expect(posterFrame(s7GuiAutomation, t)).toBe(s7Timeline(t, STUDIO_FPS).durationInFrames - 1);
  });

  it("keeps exploring after a traversal starts", () => {
    const json = traceJson();
    json.events.push({ id: "e90", at: "2026-09-19T10:03:40Z", kind: "gui.transition_found", transition: "t-search" } as never);
    json.events = json.events.filter((e) => e.id !== "e05");
    const t = parseTrace(json);
    const ttl = s7Timeline(t, STUDIO_FPS);
    const end = s7State(t, ttl, ttl.durationInFrames - 1);
    expect(end.phase).toBe("traversing");
    expect(end.edges.find((e) => e.id === "t-search")?.level).toBe("testing");
  });
});
