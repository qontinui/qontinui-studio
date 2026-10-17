/**
 * Compositions (plan Phase 5): the teaser, the explainer and the factory
 * pulse, built from scenes WITHOUT changing them. A composition is a list of
 * segments played back to back:
 *
 * - `scene`: a window of one scene, driven by a trace, at a playback rate;
 * - `card`: a title or closing card (text from the definition, with
 *   `{{var}}` placeholders filled at compose time);
 * - `broll`: an AI b-roll still in a named slot. A slot with no image bound
 *   is OMITTED (and the omission recorded), so every composition renders with
 *   zero b-roll and accepts it later.
 *
 * `resolveProgram` turns a definition, its traces, the bound b-roll and an
 * optional narration timing table into a `Program`: plain JSON that an engine
 * adapter plays frame by frame (`programFrameAt`). Narration only stretches
 * or warps a segment's playback; it never edits a scene:
 *
 * - a narrated segment lasts at least as long as its clip plus a lead and a
 *   tail; the scene plays at its rate and then holds its last frame;
 * - a mark named after an event id in the segment's trace pins that event's
 *   beat to the moment the mark is spoken (piecewise-linear time warp between
 *   pins). Marks naming nothing in the trace are ignored, and listed; a mark
 *   that would make the scene run faster than 4x or slower than 0.25x to
 *   reach its beat is refused.
 *
 * Everything here is a pure function of its inputs: two resolutions of the
 * same inputs are identical, so a composed video is as reproducible as a
 * scene render.
 */
import { STUDIO_FPS } from "./layout";
import { msToFrames } from "./motion";
import type { SegmentNarration, NarrationTiming } from "./narration-timing";
import type { SceneDefinition } from "./scene";
import { describeCharacter, uncoveredCharacters } from "./text-coverage";
import type { SceneTrace } from "./trace/schema";

// ---------------------------------------------------------------------------
// Definitions
// ---------------------------------------------------------------------------

export interface SceneSegmentDefinition {
  kind: "scene";
  /** Segment id: unique in the composition; the narration script's `## <id>`. */
  id: string;
  scene: string;
  /** Playback speed (1 = the scene's own pace; 2 = twice as fast). Default 1. */
  rate?: number;
  /** Frames of the scene's own timeline to play, `[from, to)`. Default: all. */
  window?: { from?: number; to?: number };
}

export interface CardSegmentDefinition {
  kind: "card";
  id: string;
  /** `{{name}}` placeholders are filled from compose-time variables. */
  title: string;
  body?: string;
  seconds: number;
}

export interface BrollSegmentDefinition {
  kind: "broll";
  id: string;
  /** The slot an image is bound to at compose time; unbound slots are omitted. */
  slot: string;
  seconds: number;
  /** One line drawn under the image, e.g. what it depicts. */
  caption?: string;
}

export type SegmentDefinition = SceneSegmentDefinition | CardSegmentDefinition | BrollSegmentDefinition;

export interface CompositionDefinition {
  /** Stable id used by the CLI, e.g. "explainer". */
  id: string;
  title: string;
  description: string;
  segments: readonly SegmentDefinition[];
}

const ID = /^[a-z0-9][a-z0-9-]*$/;

/** The playback speeds a segment may run at, set by its rate or by narration marks. */
export const MIN_RATE = 0.25;
export const MAX_RATE = 4;

export function defineComposition(definition: CompositionDefinition): CompositionDefinition {
  if (!ID.test(definition.id)) throw new Error(`composition id "${definition.id}" must match [a-z0-9-]+`);
  const seen = new Set<string>();
  for (const segment of definition.segments) {
    if (!ID.test(segment.id)) throw new Error(`${definition.id}: segment id "${segment.id}" must match [a-z0-9-]+`);
    if (seen.has(segment.id)) throw new Error(`${definition.id}: segment id "${segment.id}" is used twice`);
    seen.add(segment.id);
    if (segment.kind === "scene" && segment.rate !== undefined && !(segment.rate >= MIN_RATE && segment.rate <= MAX_RATE)) {
      throw new Error(`${definition.id}/${segment.id}: rate must be within [${MIN_RATE}, ${MAX_RATE}]`);
    }
    if (segment.kind !== "scene" && !(segment.seconds > 0 && segment.seconds <= 60)) {
      throw new Error(`${definition.id}/${segment.id}: seconds must be within (0, 60]`);
    }
  }
  if (!definition.segments.some((s) => s.kind === "scene")) {
    throw new Error(`${definition.id}: a composition needs at least one scene segment`);
  }
  return definition;
}

/** The `{{name}}` placeholders in a definition's cards, in order of appearance. */
export function compositionVariables(definition: CompositionDefinition): string[] {
  const names: string[] = [];
  for (const segment of definition.segments) {
    if (segment.kind !== "card") continue;
    for (const text of [segment.title, segment.body ?? ""]) {
      for (const match of text.matchAll(/\{\{([a-z][a-z0-9_]*)\}\}/g)) {
        const name = match[1] as string;
        if (!names.includes(name)) names.push(name);
      }
    }
  }
  return names;
}

// ---------------------------------------------------------------------------
// The program: what an adapter plays
// ---------------------------------------------------------------------------

/** A pin of a scene's time warp: output frame `out` (segment-local) shows scene frame `scene`. */
export type WarpPin = readonly [out: number, scene: number];

export interface ProgramSceneSegment {
  kind: "scene";
  id: string;
  startFrame: number;
  durationInFrames: number;
  sceneId: string;
  trace: SceneTrace;
  /** The scene frames played: `[from, to)`. */
  from: number;
  to: number;
  rate: number;
  /** Increasing in both coordinates; the first pin is `[0, from]`. */
  pins: readonly WarpPin[];
}

export interface ProgramCardSegment {
  kind: "card";
  id: string;
  startFrame: number;
  durationInFrames: number;
  title: string;
  body?: string;
  /** The mode label a card carries: its nearest scene's (plan D2). */
  meta: SceneTrace["meta"];
}

export interface ProgramBrollSegment {
  kind: "broll";
  id: string;
  startFrame: number;
  durationInFrames: number;
  slot: string;
  /** Where the adapter serves the image from (a published media name). */
  src: string;
  caption?: string;
  meta: SceneTrace["meta"];
}

export type ProgramSegment = ProgramSceneSegment | ProgramCardSegment | ProgramBrollSegment;

export interface ProgramAudioClip {
  segmentId: string;
  /** A published media name the adapter serves. */
  src: string;
  startFrame: number;
  durationInFrames: number;
}

export interface Program {
  compositionId: string;
  fps: number;
  durationInFrames: number;
  segments: readonly ProgramSegment[];
  audio: readonly ProgramAudioClip[];
  /** Segments left out, and why (e.g. a b-roll slot with no image bound). */
  omitted: readonly { id: string; reason: string }[];
  /** Notes worth a reader's attention (e.g. marks that pinned nothing). */
  notes: readonly string[];
}

/** A b-roll image bound to a slot, already published under `src`. */
export interface BrollBinding {
  src: string;
}

/** A narration clip, already published under `src`. */
export interface NarrationBinding {
  src: string;
  narration: SegmentNarration;
}

export interface ResolveInputs {
  definition: CompositionDefinition;
  /** Scene definitions by id (the registry). */
  scenes: Readonly<Record<string, SceneDefinition>>;
  /** A trace for every scene segment, by segment id. */
  traces: Readonly<Record<string, SceneTrace>>;
  /** Card placeholder values. Every placeholder must be given. */
  variables?: Readonly<Record<string, string>>;
  /** Bound b-roll, by slot. */
  broll?: Readonly<Record<string, BrollBinding>>;
  /** Narration clips, by segment id. */
  narration?: Readonly<Record<string, NarrationBinding>>;
  fps?: number;
}

/** Silence before a segment's narration starts, and after it ends. */
export const NARRATION_LEAD_MS = 250;
export const NARRATION_TAIL_MS = 500;

/** Card text never wraps past this, so a variable cannot overflow the card. */
export const CARD_TITLE_MAX = 80;
export const CARD_BODY_MAX = 160;

export class CompositionError extends Error {
  override readonly name = "CompositionError";
}

function fill(text: string, variables: Readonly<Record<string, string>>, where: string): string {
  return text.replace(/\{\{([a-z][a-z0-9_]*)\}\}/g, (_, name: string) => {
    const value = variables[name];
    if (value === undefined) throw new CompositionError(`${where}: variable "${name}" is not set (pass --var ${name}=<value>)`);
    return value;
  });
}

function checkText(text: string, max: number, where: string): void {
  if (text.length > max) throw new CompositionError(`${where}: ${text.length} characters, at most ${max}`);
  const missing = uncoveredCharacters(text);
  if (missing.length) {
    throw new CompositionError(`${where}: the bundled fonts cannot draw ${missing.map(describeCharacter).join(", ")}`);
  }
}

/**
 * The scene frames a segment plays and how the output maps onto them.
 * `natural` is the length at `rate` with no narration.
 */
function scenePins(
  segment: SceneSegmentDefinition,
  scene: SceneDefinition,
  trace: SceneTrace,
  fps: number,
  narration: SegmentNarration | undefined,
  notes: string[],
): { from: number; to: number; rate: number; pins: WarpPin[]; durationInFrames: number } {
  const timeline = scene.timeline(trace, fps);
  const total = timeline.durationInFrames;
  const from = segment.window?.from ?? 0;
  const to = segment.window?.to ?? total;
  if (!Number.isInteger(from) || !Number.isInteger(to) || from < 0 || to > total || to - from < 1) {
    throw new CompositionError(`segment "${segment.id}": window [${from}, ${to}) is not inside 0..${total} for this trace`);
  }
  const rate = segment.rate ?? 1;
  const pins: WarpPin[] = [[0, from]];

  if (narration) {
    const lead = msToFrames(NARRATION_LEAD_MS, fps);
    const beatById = new Map(timeline.beats.map((beat) => [beat.event.id, beat.startFrame]));
    const pinned = Object.entries(narration.marks)
      .map(([name, at]) => ({ name, out: lead + Math.round(at * fps), scene: beatById.get(name) }))
      .filter((mark) => {
        if (mark.scene === undefined) {
          notes.push(`segment "${segment.id}": mark [#${mark.name}] names no event of its trace; it pins nothing`);
          return false;
        }
        if ((mark.scene as number) === from) {
          notes.push(`segment "${segment.id}": mark [#${mark.name}] names the beat the segment opens on, which is already pinned to its start`);
          return false;
        }
        if ((mark.scene as number) < from || (mark.scene as number) >= to) {
          notes.push(`segment "${segment.id}": mark [#${mark.name}] names a beat outside the segment's window; it pins nothing`);
          return false;
        }
        return true;
      })
      .sort((a, b) => a.out - b.out || (a.scene as number) - (b.scene as number));
    for (const mark of pinned) {
      const [lastOut, lastScene] = pins[pins.length - 1] as WarpPin;
      if (mark.out <= lastOut || (mark.scene as number) <= lastScene) {
        throw new CompositionError(
          `segment "${segment.id}": mark [#${mark.name}] is spoken out of order with the scene's beats ` +
            "(marks must name beats in the order the scene plays them, and at least a frame apart)",
        );
      }
      const speed = ((mark.scene as number) - lastScene) / (mark.out - lastOut);
      if (speed < MIN_RATE || speed > MAX_RATE) {
        throw new CompositionError(
          `segment "${segment.id}": mark [#${mark.name}] would play the scene at ${speed.toFixed(2)}x to reach its beat ` +
            `(allowed ${MIN_RATE}x to ${MAX_RATE}x); move the mark in the script`,
        );
      }
      pins.push([mark.out, mark.scene as number]);
    }
  }

  // After the last pin the scene plays at `rate` to its window's end, then holds.
  const [lastOut, lastScene] = pins[pins.length - 1] as WarpPin;
  const played = lastOut + Math.ceil((to - lastScene) / rate);
  let durationInFrames = Math.max(1, played);
  if (narration) {
    const need =
      msToFrames(NARRATION_LEAD_MS, fps) + Math.ceil(narration.duration_seconds * fps) + msToFrames(NARRATION_TAIL_MS, fps);
    durationInFrames = Math.max(durationInFrames, need);
  }
  return { from, to, rate, pins, durationInFrames };
}

function nearestSceneMeta(definition: CompositionDefinition, index: number, traces: Readonly<Record<string, SceneTrace>>) {
  const order = [
    ...definition.segments.slice(index + 1),
    ...definition.segments.slice(0, index).reverse(),
  ];
  const next = order.find((s) => s.kind === "scene");
  return (traces[(next as SegmentDefinition).id] as SceneTrace).meta;
}

export function resolveProgram(inputs: ResolveInputs): Program {
  const { definition, scenes } = inputs;
  const fps = inputs.fps ?? STUDIO_FPS;
  const variables = inputs.variables ?? {};
  const broll = inputs.broll ?? {};
  const narration = inputs.narration ?? {};
  const known = new Set(definition.segments.map((s) => s.id));
  for (const id of Object.keys(narration)) {
    if (!known.has(id)) throw new CompositionError(`narration names segment "${id}", which ${definition.id} does not have`);
  }
  for (const name of Object.keys(variables)) {
    if (!compositionVariables(definition).includes(name)) {
      throw new CompositionError(`variable "${name}" is not used by ${definition.id}`);
    }
  }
  const slots = new Set(definition.segments.flatMap((s) => (s.kind === "broll" ? [s.slot] : [])));
  for (const slot of Object.keys(broll)) {
    if (!slots.has(slot)) throw new CompositionError(`b-roll slot "${slot}" is not a slot of ${definition.id} (slots: ${[...slots].join(", ") || "none"})`);
  }

  const segments: ProgramSegment[] = [];
  const audio: ProgramAudioClip[] = [];
  const omitted: { id: string; reason: string }[] = [];
  const notes: string[] = [];
  let cursor = 0;

  definition.segments.forEach((segment, index) => {
    const voice = narration[segment.id];
    const where = `${definition.id}/${segment.id}`;
    if (segment.kind === "broll" && !broll[segment.slot]) {
      omitted.push({ id: segment.id, reason: `b-roll slot "${segment.slot}" has no image bound` });
      if (voice) throw new CompositionError(`${where}: narrated, but its b-roll slot has no image; narrate a segment that plays`);
      return;
    }
    let placed: ProgramSegment;
    if (segment.kind === "scene") {
      const scene = scenes[segment.scene];
      if (!scene) throw new CompositionError(`${where}: unknown scene "${segment.scene}"`);
      const trace = inputs.traces[segment.id];
      if (!trace) throw new CompositionError(`${where}: no trace given`);
      const shape = scenePins(segment, scene, trace, fps, voice?.narration, notes);
      placed = { kind: "scene", id: segment.id, startFrame: cursor, sceneId: scene.id, trace, ...shape };
    } else {
      const base = Math.max(1, Math.round(segment.seconds * fps));
      const need = voice
        ? msToFrames(NARRATION_LEAD_MS, fps) + Math.ceil(voice.narration.duration_seconds * fps) + msToFrames(NARRATION_TAIL_MS, fps)
        : 0;
      const durationInFrames = Math.max(base, need);
      const meta = nearestSceneMeta(definition, index, inputs.traces);
      if (segment.kind === "card") {
        const title = fill(segment.title, variables, `${where} title`);
        checkText(title, CARD_TITLE_MAX, `${where} title`);
        const body = segment.body === undefined ? undefined : fill(segment.body, variables, `${where} body`);
        if (body !== undefined) checkText(body, CARD_BODY_MAX, `${where} body`);
        placed = { kind: "card", id: segment.id, startFrame: cursor, durationInFrames, title, ...(body === undefined ? {} : { body }), meta };
      } else {
        if (segment.caption !== undefined) checkText(segment.caption, CARD_BODY_MAX, `${where} caption`);
        placed = {
          kind: "broll",
          id: segment.id,
          startFrame: cursor,
          durationInFrames,
          slot: segment.slot,
          src: (broll[segment.slot] as BrollBinding).src,
          ...(segment.caption === undefined ? {} : { caption: segment.caption }),
          meta,
        };
      }
    }
    if (voice) {
      audio.push({
        segmentId: segment.id,
        src: voice.src,
        startFrame: cursor + msToFrames(NARRATION_LEAD_MS, fps),
        durationInFrames: Math.ceil(voice.narration.duration_seconds * fps),
      });
    }
    segments.push(placed);
    cursor += placed.durationInFrames;
  });

  for (const segment of definition.segments) {
    if (segment.kind === "scene" && !narration[segment.id] && Object.keys(narration).length > 0) {
      notes.push(`segment "${segment.id}" has no narration`);
    }
  }
  return { compositionId: definition.id, fps, durationInFrames: cursor, segments, audio, omitted, notes };
}

/** The scene frame a scene segment shows at segment-local frame `local`. */
export function warpedSceneFrame(segment: ProgramSceneSegment, local: number): number {
  const { pins, from, to, rate } = segment;
  let scene: number;
  const last = pins[pins.length - 1] as WarpPin;
  if (local >= last[0]) {
    scene = last[1] + (local - last[0]) * rate;
  } else {
    let i = 0;
    while (i + 1 < pins.length && (pins[i + 1] as WarpPin)[0] <= local) i++;
    const [o0, s0] = pins[i] as WarpPin;
    const [o1, s1] = pins[i + 1] as WarpPin;
    scene = s0 + ((local - o0) * (s1 - s0)) / (o1 - o0);
  }
  return Math.min(to - 1, Math.max(from, Math.floor(scene + 1e-9)));
}

export type ProgramFrame =
  | { segment: ProgramSceneSegment; local: number; sceneFrame: number }
  | { segment: ProgramCardSegment | ProgramBrollSegment; local: number };

/** What the program shows at `frame` (clamped into the program). */
export function programFrameAt(program: Program, frame: number): ProgramFrame {
  const f = Math.min(Math.max(0, frame), program.durationInFrames - 1);
  let segment = program.segments[0] as ProgramSegment;
  for (const candidate of program.segments) {
    if (candidate.startFrame <= f) segment = candidate;
    else break;
  }
  const local = f - segment.startFrame;
  if (segment.kind === "scene") return { segment, local, sceneFrame: warpedSceneFrame(segment, local) };
  return { segment, local };
}

/** Where each segment sits in the output, for manifests and the publish gate. */
export function programOutline(program: Program) {
  return program.segments.map((segment) => ({
    id: segment.id,
    kind: segment.kind,
    startFrame: segment.startFrame,
    durationInFrames: segment.durationInFrames,
    ...(segment.kind === "scene"
      ? { scene: segment.sceneId, mode: segment.trace.meta.mode, window: [segment.from, segment.to] as const }
      : { mode: segment.meta.mode }),
  }));
}

/** Narration clips by segment id, from a timing table and a publish function. */
export function narrationBindings(
  timing: NarrationTiming,
  publish: (segmentId: string, narration: SegmentNarration) => string,
): Record<string, NarrationBinding> {
  return Object.fromEntries(
    Object.entries(timing.segments).map(([id, narration]) => [id, { src: publish(id, narration), narration }]),
  );
}
