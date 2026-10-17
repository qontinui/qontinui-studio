/**
 * The scene contract. A scene is a PURE function of `(trace, frame)`: its
 * component receives the validated trace, the frame number, the layout box
 * and the timeline built from the trace, and renders from those alone. It
 * never reads a clock, never schedules work, and never imports an engine.
 */
import type { ComponentType } from "react";

import type { Layout } from "./layout";
import type { Timeline } from "./timeline";
import type { EventKind, SceneTrace } from "./trace/schema";

export type EntityKind = "plans" | "prs" | "sessions";

export interface SceneRequirements {
  /** Each kind must occur at least once in the trace. */
  kinds: readonly EventKind[];
  /** Each entity list must be non-empty. */
  entities: readonly EntityKind[];
}

export interface SceneProps {
  trace: SceneTrace;
  frame: number;
  layout: Layout;
  timeline: Timeline;
}

export interface SceneDefinition {
  /** Stable id used by the CLI and composition ids, e.g. "s1-plan-lifecycle". */
  id: string;
  title: string;
  /** The trace fields this scene reads (plan Phase 2: scenes declare them). */
  requires: SceneRequirements;
  /** Scene-specific shape checks (e.g. "exactly one plan"); empty when fine. */
  check?: (trace: SceneTrace) => string[];
  /** Trace → timeline; determines the render's duration. */
  timeline: (trace: SceneTrace, fps: number) => Timeline;
  /**
   * The poster frame: what an embed shows before it plays, and all it shows
   * under `prefers-reduced-motion: reduce` until the viewer presses play.
   * Choose a frame that tells the scene's story on its own. Default: the
   * last frame. Clamped to the scene's duration.
   */
  poster?: (timeline: Timeline) => number;
  Component: ComponentType<SceneProps>;
}

export function defineScene(definition: SceneDefinition): SceneDefinition {
  if (!/^[a-z0-9-]+$/.test(definition.id)) {
    throw new Error(`scene id "${definition.id}" must match [a-z0-9-]+`);
  }
  return definition;
}

/** Problems that stop `scene` from rendering `trace`; empty when it can. */
export function missingRequirements(scene: SceneDefinition, trace: SceneTrace): string[] {
  const present = new Set(trace.events.map((e) => e.kind));
  const missing: string[] = [];
  for (const kind of scene.requires.kinds) {
    if (!present.has(kind)) missing.push(`event kind "${kind}"`);
  }
  for (const entity of scene.requires.entities) {
    if (trace.entities[entity].length === 0) missing.push(`entities.${entity}`);
  }
  if (scene.check) missing.push(...scene.check(trace));
  return missing;
}
