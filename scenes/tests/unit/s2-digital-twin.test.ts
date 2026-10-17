import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { STUDIO_FPS, findBeat, missingRequirements, parseTrace, validateTrace, type SceneTraceInput } from "../../src/api";
import { posterFrame } from "../../src/api/scene-root";
import { s2DigitalTwin } from "../../src/scenes/s2-digital-twin";
import { DECISION_ROWS, TWIN_ROWS, s2State, s2Timeline } from "../../src/scenes/s2-digital-twin/model";

const here = path.dirname(fileURLToPath(import.meta.url));
const traceJson = (): SceneTraceInput =>
  JSON.parse(readFileSync(path.join(here, "../../traces/illustrative/s2-digital-twin.json"), "utf8")) as SceneTraceInput;

const errorsOf = (input: unknown) => {
  const result = validateTrace(input);
  return result.ok ? [] : result.errors;
};

describe("S2 trace fields", () => {
  it("accepts the committed illustrative trace and it drives the scene", () => {
    const trace = parseTrace(traceJson());
    expect(missingRequirements(s2DigitalTwin, trace)).toEqual([]);
  });

  it("refuses a mirrored fact from an unknown source", () => {
    const json = traceJson();
    (json.events[0] as { source: string }).source = "src-nowhere";
    expect(errorsOf(json)).toContain('events.0.source: unknown source "src-nowhere"');
  });

  it("refuses an answer that points at something other than a question", () => {
    const json = traceJson();
    (json.events[9] as { question: string }).question = "e01";
    expect(errorsOf(json)).toContain('events.9.question: "e01" is not a question.asked event');
  });

  it("refuses an answer given before its question was asked", () => {
    const json = traceJson();
    json.events[9]!.at = "2026-09-14T19:00:00Z";
    expect(errorsOf(json)).toContain('events.9.at: answered before question "e09" was asked');
  });

  it("caps the scene at four sources", () => {
    const json = traceJson();
    json.entities!.sources!.push({ id: "src-5", kind: "ci", label: "Another CI" });
    expect(missingRequirements(s2DigitalTwin, parseTrace(json))).toContain("at most four sources (the trace has 5)");
  });
});

describe("S2 scene model", () => {
  const trace = parseTrace(traceJson());
  const tl = s2Timeline(trace, STUDIO_FPS);
  const last = tl.durationInFrames - 1;

  it("starts with every source inert and nothing in the twin", () => {
    const state = s2State(trace, tl, 0);
    expect(state.sources.every((s) => s.level === "inert" && s.pulse === 0)).toBe(true);
    expect(state.twin).toEqual([]);
    expect(state.caption).toBeNull();
  });

  it("lights the reporting source and adds its fact to the twin", () => {
    const ci = findBeat(tl, (e) => e.kind === "twin.mirrored" && e.source === "src-ci")!;
    const state = s2State(trace, tl, ci.startFrame + 5);
    expect(state.sources.find((s) => s.id === "src-ci")?.level).toBe("running");
    expect(state.twin[state.twin.length - 1]?.text).toBe("PR #412: 14 of 14 checks green");
    expect(state.twin[state.twin.length - 1]?.detail).toBe("from CI");
    expect(state.caption?.title).toBe("Mirrored into the twin");
  });

  it("keeps each panel to its capacity", () => {
    const state = s2State(trace, tl, last);
    expect(state.twin.length).toBeLessThanOrEqual(TWIN_ROWS);
    expect(state.decisions.length).toBeLessThanOrEqual(DECISION_ROWS);
    expect(state.factCount).toBe(6);
  });

  it("marks a question waiting until it is answered from a served document", () => {
    const asked = findBeat(tl, (e) => e.kind === "question.asked")!;
    expect(s2State(trace, tl, asked.startFrame).developer.at(-1)?.level).toBe("waiting");
    const answered = findBeat(tl, (e) => e.kind === "question.answered")!;
    const state = s2State(trace, tl, answered.startFrame);
    expect(state.developer.map((r) => r.level)).toEqual(["done", "done"]);
    expect(state.developer.at(-1)?.detail).toBe("from served policy git-operations");
    expect(state.caption?.title).toBe("Answered from served policy");
  });

  it("never wakes the operator when documents answer, and says so when one must", () => {
    expect(s2State(trace, tl, last).operator).toEqual({ level: "inert", sublabel: "not woken · 2 answers from documents" });
    const json = traceJson();
    (json.events[11] as { from: string }).from = "operator";
    const woken = parseTrace(json);
    const wokenTl = s2Timeline(woken, STUDIO_FPS);
    const state = s2State(woken, wokenTl, wokenTl.durationInFrames - 1);
    expect(state.operator).toEqual({ level: "attention", sublabel: "woken to answer" });
    expect(state.developer.at(-1)?.detail).toBe("from the operator");
  });

  it("puts the poster on the last answer, with every panel full", () => {
    const poster = posterFrame(s2DigitalTwin, trace);
    const answers = tl.beats.filter((b) => b.event.kind === "question.answered");
    expect(poster).toBe(answers[answers.length - 1]!.startFrame + tl.beatFrames - 1);
    const state = s2State(trace, tl, poster);
    expect(state.decisions).toHaveLength(DECISION_ROWS);
    expect(state.developer).toHaveLength(2);
  });
});
