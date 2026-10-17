/**
 * Compositions (plan Phase 5): program resolution, the narration time warp,
 * captions and the timing-table schema. Pure; no browser.
 */
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { captionCues, toWebVtt } from "../../src/api/captions";
import {
  CompositionError,
  NARRATION_LEAD_MS,
  compositionVariables,
  defineComposition,
  programFrameAt,
  resolveProgram,
  warpedSceneFrame,
  type CompositionDefinition,
  type ProgramSceneSegment,
} from "../../src/api/composition";
import { STUDIO_FPS } from "../../src/api/layout";
import { msToFrames } from "../../src/api/motion";
import { validateTiming, type NarrationTiming, type SegmentNarration } from "../../src/api/narration-timing";
import { sceneDuration } from "../../src/api/scene-root";
import { parseTrace, type SceneTrace } from "../../src/api/trace/schema";
import { compositions } from "../../src/compositions";
import { scenes } from "../../src/scenes";
import { illustrativeTraceFile } from "../golden/cases";

const SHA = "0".repeat(64);

function trace(sceneId: string): SceneTrace {
  return parseTrace(JSON.parse(readFileSync(illustrativeTraceFile(sceneId), "utf8")));
}

function tracesFor(definition: CompositionDefinition): Record<string, SceneTrace> {
  return Object.fromEntries(
    definition.segments.flatMap((s) => (s.kind === "scene" ? [[s.id, trace(s.scene)]] : [])),
  );
}

function clip(duration: number, marks: Record<string, number> = {}, words: SegmentNarration["words"] = []): SegmentNarration {
  return { audio: "clip.wav", audio_sha256: SHA, duration_seconds: duration, marks, words };
}

const s1Only = defineComposition({
  id: "s1-only",
  title: "S1",
  description: "test",
  segments: [
    { kind: "card", id: "open", title: "Hello {{who}}", seconds: 1 },
    { kind: "scene", id: "s1", scene: "s1-plan-lifecycle" },
    { kind: "broll", id: "b", slot: "slot-a", seconds: 2 },
  ],
});

describe("defineComposition", () => {
  it("refuses duplicate segment ids, bad rates and a composition without a scene", () => {
    expect(() =>
      defineComposition({ id: "x", title: "x", description: "x", segments: [{ kind: "scene", id: "a", scene: "s" }, { kind: "scene", id: "a", scene: "s" }] }),
    ).toThrow(/used twice/);
    expect(() =>
      defineComposition({ id: "x", title: "x", description: "x", segments: [{ kind: "scene", id: "a", scene: "s", rate: 8 }] }),
    ).toThrow(/rate/);
    expect(() =>
      defineComposition({ id: "x", title: "x", description: "x", segments: [{ kind: "card", id: "a", title: "t", seconds: 1 }] }),
    ).toThrow(/at least one scene/);
  });

  it("lists the card variables", () => {
    expect(compositionVariables(s1Only)).toEqual(["who"]);
    expect(compositionVariables(compositions["factory-pulse"]!)).toEqual(["week", "status_line"]);
  });
});

describe("the registered compositions", () => {
  it.each(Object.keys(compositions))("%s resolves from illustrative traces with zero b-roll", (id) => {
    const definition = compositions[id]!;
    const variables = Object.fromEntries(compositionVariables(definition).map((name) => [name, "2026-W41"]));
    const program = resolveProgram({ definition, scenes, traces: tracesFor(definition), variables });
    expect(program.durationInFrames).toBeGreaterThan(0);
    // Every b-roll slot is omitted, and says why.
    const slots = definition.segments.filter((s) => s.kind === "broll");
    expect(program.omitted.map((o) => o.id)).toEqual(slots.map((s) => s.id));
    // Segments tile the program with no gap.
    let cursor = 0;
    for (const segment of program.segments) {
      expect(segment.startFrame).toBe(cursor);
      cursor += segment.durationInFrames;
    }
    expect(cursor).toBe(program.durationInFrames);
  });

  it("the teaser is about 30 s and the explainer at least a minute before narration", () => {
    const length = (id: string) => {
      const definition = compositions[id]!;
      return resolveProgram({ definition, scenes, traces: tracesFor(definition) }).durationInFrames / STUDIO_FPS;
    };
    expect(length("teaser")).toBeGreaterThan(27);
    expect(length("teaser")).toBeLessThan(33);
    expect(length("explainer")).toBeGreaterThan(60);
  });

  it("a card carries its nearest scene's mode label", () => {
    const definition = compositions.teaser!;
    const program = resolveProgram({ definition, scenes, traces: tracesFor(definition) });
    const open = program.segments.find((s) => s.id === "open");
    expect(open?.kind === "card" && open.meta.mode).toBe("illustrative");
  });
});

describe("resolveProgram refusals", () => {
  const traces = tracesFor(s1Only);
  it("needs every card variable, and only known ones", () => {
    expect(() => resolveProgram({ definition: s1Only, scenes, traces })).toThrow(/variable "who" is not set/);
    expect(() => resolveProgram({ definition: s1Only, scenes, traces, variables: { who: "x", other: "y" } })).toThrow(/"other" is not used/);
  });
  it("refuses card text the fonts cannot draw, or too long", () => {
    expect(() => resolveProgram({ definition: s1Only, scenes, traces, variables: { who: "日本" } })).toThrow(CompositionError);
    expect(() => resolveProgram({ definition: s1Only, scenes, traces, variables: { who: "x".repeat(200) } })).toThrow(/at most 80/);
  });
  it("refuses an unknown b-roll slot and narration for an unknown segment", () => {
    expect(() => resolveProgram({ definition: s1Only, scenes, traces, variables: { who: "x" }, broll: { nope: { src: "media/a.png" } } })).toThrow(/not a slot/);
    expect(() =>
      resolveProgram({ definition: s1Only, scenes, traces, variables: { who: "x" }, narration: { ghost: { src: "media/a.wav", narration: clip(1) } } }),
    ).toThrow(/does not have/);
  });
  it("refuses narration over an omitted b-roll slot", () => {
    expect(() =>
      resolveProgram({ definition: s1Only, scenes, traces, variables: { who: "x" }, narration: { b: { src: "media/a.wav", narration: clip(1) } } }),
    ).toThrow(/no image/);
  });
  it("binds b-roll when given", () => {
    const program = resolveProgram({ definition: s1Only, scenes, traces, variables: { who: "x" }, broll: { "slot-a": { src: "media/a.png" } } });
    expect(program.omitted).toEqual([]);
    expect(program.segments.at(-1)).toMatchObject({ kind: "broll", src: "media/a.png", durationInFrames: 2 * STUDIO_FPS });
  });
});

describe("scene playback", () => {
  const s1 = scenes["s1-plan-lifecycle"]!;
  const s1Trace = trace("s1-plan-lifecycle");
  const natural = sceneDuration(s1, s1Trace);
  const one = (definition: CompositionDefinition, narration?: Record<string, SegmentNarration>) =>
    resolveProgram({
      definition,
      scenes,
      traces: tracesFor(definition),
      ...(narration ? { narration: Object.fromEntries(Object.entries(narration).map(([k, v]) => [k, { src: "media/n.wav", narration: v }])) } : {}),
    });

  it("plays the whole scene frame for frame at rate 1", () => {
    const program = one(defineComposition({ id: "a", title: "a", description: "a", segments: [{ kind: "scene", id: "s", scene: s1.id }] }));
    const segment = program.segments[0] as ProgramSceneSegment;
    expect(segment.durationInFrames).toBe(natural);
    for (const f of [0, 1, 100, natural - 1]) expect(warpedSceneFrame(segment, f)).toBe(f);
  });

  it("plays twice as fast at rate 2 and stays inside its window", () => {
    const program = one(
      defineComposition({ id: "a", title: "a", description: "a", segments: [{ kind: "scene", id: "s", scene: s1.id, rate: 2, window: { from: 30, to: 230 } }] }),
    );
    const segment = program.segments[0] as ProgramSceneSegment;
    expect(segment.durationInFrames).toBe(100);
    expect(warpedSceneFrame(segment, 0)).toBe(30);
    expect(warpedSceneFrame(segment, 10)).toBe(50);
    expect(warpedSceneFrame(segment, 99)).toBe(228);
  });

  it("holds the last frame while a longer narration finishes", () => {
    const definition = defineComposition({ id: "a", title: "a", description: "a", segments: [{ kind: "scene", id: "s", scene: s1.id }] });
    const program = one(definition, { s: clip(30) });
    const segment = program.segments[0] as ProgramSceneSegment;
    expect(segment.durationInFrames).toBeGreaterThan(30 * STUDIO_FPS);
    expect(warpedSceneFrame(segment, segment.durationInFrames - 1)).toBe(natural - 1);
    expect(program.audio).toEqual([{ segmentId: "s", src: "media/n.wav", startFrame: msToFrames(NARRATION_LEAD_MS, STUDIO_FPS), durationInFrames: 30 * STUDIO_FPS }]);
  });

  it("pins a beat to the moment its mark is spoken", () => {
    const definition = defineComposition({ id: "a", title: "a", description: "a", segments: [{ kind: "scene", id: "s", scene: s1.id }] });
    const timeline = s1.timeline(s1Trace, STUDIO_FPS);
    const beat = timeline.beats[5]!;
    const program = one(definition, { s: clip(12, { [beat.event.id]: 9 }) });
    const segment = program.segments[0] as ProgramSceneSegment;
    const out = msToFrames(NARRATION_LEAD_MS, STUDIO_FPS) + 9 * STUDIO_FPS;
    expect(warpedSceneFrame(segment, out)).toBe(beat.startFrame);
    expect(warpedSceneFrame(segment, out - 1)).toBeLessThan(beat.startFrame);
    // Monotonic across the whole segment.
    let previous = -1;
    for (let f = 0; f < segment.durationInFrames; f++) {
      const sceneFrame = warpedSceneFrame(segment, f);
      expect(sceneFrame).toBeGreaterThanOrEqual(previous);
      previous = sceneFrame;
    }
  });

  it("refuses marks spoken out of the scene's order, and notes marks that pin nothing", () => {
    const definition = defineComposition({ id: "a", title: "a", description: "a", segments: [{ kind: "scene", id: "s", scene: s1.id }] });
    const beats = s1.timeline(s1Trace, STUDIO_FPS).beats;
    expect(() => one(definition, { s: clip(12, { [beats[6]!.event.id]: 2, [beats[3]!.event.id]: 5 }) })).toThrow(/out of order/);
    const program = one(definition, { s: clip(12, { intro: 1 }) });
    expect(program.notes.join("\n")).toMatch(/\[#intro\] names no event/);
  });

  it("refuses a mark that would make the scene run outside 0.25x..4x", () => {
    const definition = defineComposition({ id: "a", title: "a", description: "a", segments: [{ kind: "scene", id: "s", scene: s1.id, rate: 2 }] });
    const beats = s1.timeline(s1Trace, STUDIO_FPS).beats;
    // The last beat spoken 0.1 s in: the scene would have to jump ~30x.
    expect(() => one(definition, { s: clip(12, { [beats.at(-1)!.event.id]: 0.1 }) })).toThrow(/would play the scene at .*x/);
    // An early beat spoken very late: the scene would nearly freeze.
    expect(() => one(definition, { s: clip(120, { [beats[1]!.event.id]: 110 }) })).toThrow(/would play the scene at 0\.\d+x/);
  });

  it("notes a mark on the beat the window opens on", () => {
    const beats = s1.timeline(s1Trace, STUDIO_FPS).beats;
    const definition = defineComposition({
      id: "a",
      title: "a",
      description: "a",
      segments: [{ kind: "scene", id: "s", scene: s1.id, window: { from: beats[2]!.startFrame } }],
    });
    const program = one(definition, { s: clip(8, { [beats[2]!.event.id]: 1 }) });
    expect(program.notes.join("\n")).toMatch(/the beat the segment opens on/);
  });

  it("programFrameAt finds the segment and clamps", () => {
    const definition = compositions.teaser!;
    const program = resolveProgram({ definition, scenes, traces: tracesFor(definition) });
    expect(programFrameAt(program, -5).segment.id).toBe("open");
    expect(programFrameAt(program, program.durationInFrames + 10).segment.id).toBe("close");
    const s = program.segments[1]!;
    expect(programFrameAt(program, s.startFrame).segment.id).toBe(s.id);
    expect(programFrameAt(program, s.startFrame - 1).segment.id).toBe("open");
  });
});

describe("captions", () => {
  const definition = defineComposition({ id: "a", title: "a", description: "a", segments: [{ kind: "scene", id: "s", scene: "s1-plan-lifecycle" }] });
  const words = [
    { text: "A", start: 0, end: 0.2, matched: true },
    { text: "plan", start: 0.2, end: 0.5, matched: true },
    { text: "ships.", start: 0.5, end: 0.9, matched: true },
    { text: "Then", start: 2, end: 2.3, matched: true },
    { text: "<it>", start: 2.3, end: 2.6, matched: false },
  ];
  const timing: NarrationTiming = {
    timing_version: 1,
    composition: "a",
    script_sha256: SHA,
    voice: "test",
    segments: { s: clip(3, {}, words) },
  };
  const program = resolveProgram({
    definition,
    scenes,
    traces: tracesFor(definition),
    narration: { s: { src: "media/n.wav", narration: timing.segments.s! } },
  });

  it("splits cues at sentence ends and offsets them by the clip's start", () => {
    const cues = captionCues(program, timing);
    const lead = msToFrames(NARRATION_LEAD_MS, STUDIO_FPS) / STUDIO_FPS;
    expect(cues.map((c) => c.text)).toEqual(["A plan ships.", "Then <it>"]);
    expect(cues[0]!.start).toBeCloseTo(lead);
    expect(cues[1]!.end).toBeCloseTo(lead + 2.6);
  });

  it("never lets a cue outlast its clip", () => {
    const late: NarrationTiming = { ...timing, segments: { s: clip(3, {}, [{ text: "Long.", start: 2.5, end: 3, matched: true }]) } };
    const latePrograms = resolveProgram({
      definition,
      scenes,
      traces: tracesFor(definition),
      narration: { s: { src: "media/n.wav", narration: late.segments.s! } },
    });
    const clipEnd = (latePrograms.audio[0]!.startFrame + latePrograms.audio[0]!.durationInFrames) / STUDIO_FPS;
    for (const cue of captionCues(latePrograms, late)) expect(cue.end).toBeLessThanOrEqual(clipEnd + 1e-9);
  });

  it("writes escaped WebVTT", () => {
    const vtt = toWebVtt(captionCues(program, timing));
    expect(vtt.startsWith("WEBVTT\n\n1\n00:00:00.267 --> 00:00:01.167\nA plan ships.\n")).toBe(true);
    expect(vtt).toContain("Then &lt;it&gt;");
  });
});

describe("the timing table", () => {
  const good = { timing_version: 1, composition: "teaser", script_sha256: SHA, voice: "v", segments: { lifecycle: clip(4, { e05: 1 }) } };
  it("accepts a valid table", () => {
    expect(validateTiming(good).ok).toBe(true);
  });
  it("refuses a mark past the clip, a path as audio name, unordered words", () => {
    const bad = (segment: Partial<SegmentNarration>) => validateTiming({ ...good, segments: { lifecycle: { ...clip(4), ...segment } } });
    expect(bad({ marks: { e05: 9 } })).toMatchObject({ ok: false });
    expect(bad({ audio: "../x.wav" })).toMatchObject({ ok: false });
    expect(
      bad({ words: [{ text: "b", start: 1, end: 1.2, matched: true }, { text: "a", start: 0.5, end: 0.7, matched: true }] }),
    ).toMatchObject({ ok: false });
    expect(bad({ words: [{ text: "late", start: 3.5, end: 50, matched: true }] })).toMatchObject({ ok: false });
  });
});
