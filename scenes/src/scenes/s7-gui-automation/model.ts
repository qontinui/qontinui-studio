/**
 * S7 "Model-based GUI automation" — the pure model. Exploring an application
 * finds its GUI states and the transitions (actions) between them; the
 * learned graph is then traversed by actions to reach a goal. This is the
 * original Qontinui idea, now one module of the factory.
 *
 * Rendering lives in `GuiAutomation.tsx`.
 */
import {
  activeBeatIndex,
  buildTimeline,
  durations,
  easings,
  msToFrames,
  progress,
  type AttentionLevel,
  type Beat,
  type EventKind,
  type SceneTrace,
  type Timeline,
} from "../../api";

/** The event kinds that drive S7 (its beats). */
export const S7_KINDS = ["gui.state_found", "gui.transition_found", "gui.transition_taken"] as const satisfies readonly EventKind[];

export const S7_TIMING = {
  introMs: 1200,
  beatMs: 900,
  outroMs: 2200,
} as const;

/** States the layouts have slots for (the scene's check caps the trace at this). */
export const MAX_STATES = 6;
/** Rows of room in the learned-model panel. */
export const MODEL_ROWS = 4;

export function s7Timeline(trace: SceneTrace, fps: number): Timeline {
  return buildTimeline(trace, { fps, ...S7_TIMING, kinds: S7_KINDS });
}

export interface StateNode {
  id: string;
  label: string;
  /** Slot in discovery order (0..MAX_STATES-1); the layout places slots. */
  slot: number;
  level: AttentionLevel;
  appear: number;
  emphasis: number;
}

export interface TransitionEdge {
  id: string;
  fromSlot: number;
  toSlot: number;
  action: string;
  /** 0..1 how much of the edge is drawn in its state colour. */
  progress: number;
  level: AttentionLevel;
  appear: number;
}

export interface S7State {
  title: string;
  states: StateNode[];
  edges: TransitionEdge[];
  /** The learned model, newest last (at most MODEL_ROWS). */
  model: { id: string; text: string; detail: string; level: AttentionLevel; appear: number }[];
  statesLearned: number;
  actionsLearned: number;
  /** Actions taken so far in the traversal. */
  steps: number;
  phase: "exploring" | "traversing";
  caption: { title: string; body: string; appear: number } | null;
}

export function s7State(trace: SceneTrace, timeline: Timeline, frame: number): S7State {
  const fps = timeline.fps;
  const fast = msToFrames(durations.base, fps);
  const slow = msToFrames(durations.slow, fps);
  const beats = timeline.beats;
  const active = activeBeatIndex(timeline, frame);
  const passed = beats.slice(0, active + 1);
  const current = passed[passed.length - 1];
  const appearOf = (beat: Beat) => progress(frame, beat.startFrame, fast, easings.enter);

  // Slots follow discovery order over the WHOLE trace, so a state never moves.
  const slotOf = new Map<string, number>();
  for (const b of beats) {
    if (b.event.kind === "gui.state_found" && !slotOf.has(b.event.state)) slotOf.set(b.event.state, slotOf.size);
  }
  const transitionById = new Map(trace.entities.transitions.map((t) => [t.id, t]));
  const stateLabel = (id: string) => trace.entities.states.find((s) => s.id === id)?.label ?? id;

  const taken = passed.filter((b) => b.event.kind === "gui.transition_taken");
  const lastTaken = taken[taken.length - 1];
  const lastT = lastTaken && lastTaken.event.kind === "gui.transition_taken" ? transitionById.get(lastTaken.event.transition) : undefined;
  const firstTaken = taken[0];
  const firstT = firstTaken && firstTaken.event.kind === "gui.transition_taken" ? transitionById.get(firstTaken.event.transition) : undefined;
  // Where the traversal stands: the end of its latest step (it starts where its first step starts).
  const at = lastT ? lastT.to : undefined;
  const visited = new Set<string>(firstT ? [firstT.from] : []);
  for (const b of taken) {
    const t = b.event.kind === "gui.transition_taken" ? transitionById.get(b.event.transition) : undefined;
    if (t) visited.add(t.to);
  }

  // --- states -----------------------------------------------------------------------------
  const states: StateNode[] = [];
  for (const b of passed) {
    if (b.event.kind !== "gui.state_found") continue;
    const id = b.event.state;
    const fresh = current === b;
    let level: AttentionLevel = "landing";
    if (fresh) level = "testing";
    if (visited.has(id)) level = "done";
    if (id === at) level = "running";
    states.push({
      id,
      label: stateLabel(id),
      slot: slotOf.get(id) ?? 0,
      level,
      appear: appearOf(b),
      emphasis: id === at && lastTaken ? progress(frame, lastTaken.startFrame, fast) : fresh ? progress(frame, b.startFrame, fast) : 0,
    });
  }

  // --- transitions ------------------------------------------------------------------------
  const edges: TransitionEdge[] = [];
  for (const b of passed) {
    if (b.event.kind !== "gui.transition_found") continue;
    const t = transitionById.get(b.event.transition);
    if (!t) continue;
    const takenBeat = taken.find((tb) => tb.event.kind === "gui.transition_taken" && tb.event.transition === t.id);
    edges.push({
      id: t.id,
      fromSlot: slotOf.get(t.from) ?? 0,
      toSlot: slotOf.get(t.to) ?? 0,
      action: t.action,
      progress: takenBeat ? progress(frame, takenBeat.startFrame, slow) : 0,
      level: takenBeat ? (takenBeat === lastTaken ? "running" : "done") : "inert",
      appear: appearOf(b),
    });
  }

  // --- the learned model panel ---------------------------------------------------------------
  const model = passed
    .filter((b) => b.event.kind === "gui.transition_found")
    .map((b) => {
      const t = b.event.kind === "gui.transition_found" ? transitionById.get(b.event.transition) : undefined;
      return {
        id: b.event.id,
        text: t ? `${stateLabel(t.from)} → ${stateLabel(t.to)}` : b.event.id,
        detail: t ? `action: ${t.action}` : "",
        level: (t && taken.some((tb) => tb.event.kind === "gui.transition_taken" && tb.event.transition === t.id) ? "done" : "landing") as AttentionLevel,
        appear: appearOf(b),
      };
    });

  // --- caption ---------------------------------------------------------------------------------
  let caption: S7State["caption"] = null;
  if (current) {
    const e = current.event;
    const appear = appearOf(current);
    const text = (title: string, body: string) => ({ title, body: e.note ?? body, appear });
    if (e.kind === "gui.state_found") {
      caption = text("A state is found", `Exploring the app finds "${stateLabel(e.state)}" and adds it to the model.`);
    } else if (e.kind === "gui.transition_found") {
      const t = transitionById.get(e.transition);
      caption = t
        ? text("A transition is learned", `${t.action} goes from "${stateLabel(t.from)}" to "${stateLabel(t.to)}".`)
        : text("A transition is learned", "An action that moves the app between states.");
    } else if (e.kind === "gui.transition_taken") {
      const t = transitionById.get(e.transition);
      caption = t
        ? text("Traversed by an action", `${t.action}: "${stateLabel(t.from)}" to "${stateLabel(t.to)}", on the learned graph.`)
        : text("Traversed by an action", "The automation follows the learned graph.");
    }
  }

  return {
    title: trace.meta.title,
    states,
    edges,
    model: model.slice(-MODEL_ROWS),
    statesLearned: states.length,
    actionsLearned: edges.length,
    steps: taken.length,
    phase: taken.length ? "traversing" : "exploring",
    caption,
  };
}
