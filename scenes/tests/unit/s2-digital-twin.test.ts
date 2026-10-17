import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  SOURCE_LABEL_MAX,
  STUDIO_FPS,
  findBeat,
  missingRequirements,
  parseTrace,
  validateTrace,
  type SceneTraceInput,
} from "../../src/api";
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

  it("refuses an answer at the same instant but listed before its question", () => {
    const json = traceJson();
    const answer = json.events.splice(9, 1)[0]!;
    answer.at = json.events[8]!.at;
    json.events.splice(8, 0, answer);
    expect(errorsOf(json)).toContain('events.8.at: answered before question "e09" was asked');
  });

  it("refuses a second answer to one question", () => {
    const json = traceJson();
    json.events.push({ ...(json.events[9] as object), id: "e99", at: "2026-09-14T19:40:00Z" } as never);
    expect(errorsOf(json)).toContain('events.13.question: question "e09" is answered more than once');
  });

  it("requires a document for a policy or intent answer and refuses one for the operator", () => {
    const missing = traceJson();
    delete (missing.events[9] as { document?: string }).document;
    expect(errorsOf(missing)).toContain("events.9.document: an answer from policy must cite its document");
    const operator = traceJson();
    (operator.events[9] as { from: string }).from = "operator";
    expect(errorsOf(operator)).toContain("events.9.document: an operator's answer cites no document");
  });

  it(`caps a source label at ${SOURCE_LABEL_MAX} characters`, () => {
    const json = traceJson();
    json.entities!.sources![0]!.label = "x".repeat(SOURCE_LABEL_MAX);
    expect(validateTrace(json).ok).toBe(true);
    json.entities!.sources![0]!.label = "x".repeat(SOURCE_LABEL_MAX + 1);
    expect(validateTrace(json).ok).toBe(false);
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

  it("starts with every source inert, nothing in the twin and the operator simply not woken", () => {
    const state = s2State(trace, tl, 0);
    expect(state.sources.every((s) => s.level === "inert" && s.pulse === 0)).toBe(true);
    expect(state.twin).toEqual([]);
    expect(state.caption).toBeNull();
    expect(state.operator.sublabel).toBe("not woken");
    expect(state.decisionPulse).toBe(0);
    expect(state.developerPulse).toBe(0);
  });

  it("keeps a source's edge drawn through a repeat report (no blank frame)", () => {
    const repeat = tl.beats.filter((b) => b.event.kind === "twin.mirrored" && b.event.source === "src-github")[1]!;
    for (const frame of [repeat.startFrame - 1, repeat.startFrame, repeat.startFrame + 1]) {
      expect(s2State(trace, tl, frame).sources.find((s) => s.id === "src-github")?.pulse, `frame ${frame}`).toBe(1);
    }
    expect(s2State(trace, tl, repeat.startFrame + 2).sources.find((s) => s.id === "src-github")?.level).toBe("running");
  });

  it("draws the decision edge at the first decision and keeps it, with a decision caption", () => {
    const first = findBeat(tl, (e) => e.kind === "twin.decided")!;
    expect(s2State(trace, tl, first.startFrame - 1).decisionPulse).toBe(0);
    const later = s2State(trace, tl, first.startFrame + tl.beatFrames * 3);
    expect(later.decisionPulse).toBe(1);
    expect(s2State(trace, tl, first.startFrame).caption).toMatchObject({
      title: "Decided from the twin, not guessed",
      body: "Queue PR #412 in the merge train: every required check is green.",
    });
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
    expect(s2State(trace, tl, last).operator).toEqual({ level: "inert", sublabel: "not woken · 2 answers from documents", short: "not woken" });
    const json = traceJson();
    (json.events[11] as { from: string }).from = "operator";
    delete (json.events[11] as { document?: string }).document;
    const woken = parseTrace(json);
    const wokenTl = s2Timeline(woken, STUDIO_FPS);
    const state = s2State(woken, wokenTl, wokenTl.durationInFrames - 1);
    expect(state.operator).toEqual({ level: "attention", sublabel: "woken to answer", short: "woken" });
    expect(state.developer.at(-1)?.detail).toBe("from the operator");
  });

  it("puts the poster on the last answer, with every panel full", () => {
    const poster = posterFrame(s2DigitalTwin, trace);
    expect(poster).toBe(503);
    expect(tl.beats.find((b) => b.startFrame === 504)?.event.id).toBe("e13");
    const state = s2State(trace, tl, poster);
    expect(state.caption).toMatchObject({
      title: "Answered from declared intent",
      body: "Experienced programmers who run it. Cited: audience profile external-operator. The operator is not woken.",
    });
    expect(state.decisions).toHaveLength(DECISION_ROWS);
    expect(state.developer).toHaveLength(2);
  });
});
