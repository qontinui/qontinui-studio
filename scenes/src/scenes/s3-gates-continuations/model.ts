/**
 * S3 "Gates and continuations" — the pure model. A session reaches a
 * condition it cannot satisfy yet, registers a gate on it and ENDS: nothing
 * runs or polls while the condition is false. Coord observes the condition
 * in the twin and clears the gate, and a new session picks the work up.
 *
 * Rendering lives in `GatesContinuations.tsx`.
 */
import {
  activeBeatIndex,
  buildTimeline,
  durations,
  easings,
  eventTimeMs,
  formatDateTime,
  msToFrames,
  progress,
  realTimeAt,
  type AttentionLevel,
  type Beat,
  type EventKind,
  type EventOfKind,
  type SceneTrace,
  type Timeline,
} from "../../api";

/** The event kinds that drive S3 (its beats). */
export const S3_KINDS = [
  "session.started",
  "session.ended",
  "gate.registered",
  "gate.observed",
  "gate.cleared",
] as const satisfies readonly EventKind[];

export const S3_TIMING = {
  introMs: 1200,
  beatMs: 1200,
  outroMs: 2000,
} as const;

/** Rows of room in the gates panel. */
export const GATE_ROWS = 2;

export function s3Timeline(trace: SceneTrace, fps: number): Timeline {
  return buildTimeline(trace, { fps, ...S3_TIMING, kinds: S3_KINDS });
}

type S3Kind = (typeof S3_KINDS)[number];
type S3Beat<K extends S3Kind> = Beat & { event: EventOfKind<K> };

function of<K extends S3Kind>(beats: readonly Beat[], kind: K): S3Beat<K>[] {
  return beats.filter((b): b is S3Beat<K> => b.event.kind === kind);
}

/** Level of a gate: who must act. */
function gateLevel(observed: boolean, cleared: boolean): AttentionLevel {
  if (cleared) return "done";
  if (observed) return "testing";
  return "waiting";
}

export interface SessionCard {
  label: string;
  sublabel: string;
  level: AttentionLevel;
  appear: number;
}

export interface S3State {
  title: string;
  /** The gate in focus: the latest one registered. */
  gate: { label: string; level: AttentionLevel; open: number; appear: number } | null;
  /** The session that registered the gate in focus (or the first session, before any gate). */
  registrant: SessionCard | null;
  /** The session that continues the gate in focus, or a placeholder until it starts. */
  continuation: SessionCard;
  /** 0..1 the registrant → gate and gate → continuation edges. */
  edgeIn: number;
  edgeOut: number;
  gates: { id: string; text: string; detail: string; level: AttentionLevel; appear: number }[];
  /** Sessions running at the shown real time. */
  running: number;
  /** Real time the gate in focus has waited (ms); null before any gate. */
  waitedMs: number | null;
  lanes: { id: string; label: string; start: number; end: number; level: AttentionLevel }[];
  ticks: { at: number; level: AttentionLevel; appear: number }[];
  playhead: number;
  range: { fromMs: number; toMs: number };
  caption: { title: string; body: string; appear: number } | null;
}

export function s3State(trace: SceneTrace, timeline: Timeline, frame: number): S3State {
  const fps = timeline.fps;
  const fast = msToFrames(durations.base, fps);
  const slow = msToFrames(durations.slow, fps);
  const beats = timeline.beats;
  const active = activeBeatIndex(timeline, frame);
  const passed = beats.slice(0, active + 1);
  const current = passed[passed.length - 1];
  const appearOf = (beat: Beat) => progress(frame, beat.startFrame, fast, easings.enter);

  const firstMs = beats.length ? (beats[0] as Beat).atMs : 0;
  const lastMs = beats.length ? (beats[beats.length - 1] as Beat).atMs : 0;
  const span = Math.max(1, lastMs - firstMs);
  const nowMs = realTimeAt(timeline, frame);
  const axis = (ms: number) => Math.min(1, Math.max(0, (ms - firstMs) / span));

  const sessionLabel = (id: string) => trace.entities.sessions.find((s) => s.id === id)?.label ?? id;
  const condition = (id: string) => trace.entities.gates.find((g) => g.id === id)?.condition ?? id;
  const started = of(passed, "session.started");
  const ended = of(passed, "session.ended");
  const registered = of(passed, "gate.registered");
  const observed = of(passed, "gate.observed");
  const cleared = of(passed, "gate.cleared");
  const hasEnded = (session: string) => ended.some((b) => b.event.session === session);

  // --- the gate in focus: the one the latest gate-related beat names ----------------
  // (with several gates open at once, the centre follows what the caption says).
  let focusGate: string | undefined;
  for (const b of passed) {
    const e = b.event;
    if (e.kind === "gate.registered" || e.kind === "gate.observed" || e.kind === "gate.cleared") focusGate = e.gate;
    else if (e.kind === "session.started" && e.continues !== undefined) focusGate = e.continues;
  }
  const focus = registered.find((b) => b.event.gate === focusGate);
  let gate: S3State["gate"] = null;
  let registrant: SessionCard | null = null;
  const firstBeat = beats[0];
  let continuation: SessionCard = {
    label: "Next session",
    sublabel: "starts when the gate clears",
    level: "inert",
    appear: firstBeat ? appearOf(firstBeat) : 1,
  };
  let edgeIn = 0;
  let edgeOut = 0;
  let waitedMs: number | null = null;

  if (focus) {
    const id = focus.event.gate;
    const seen = observed.find((b) => b.event.gate === id);
    const clear = cleared.find((b) => b.event.gate === id);
    gate = {
      label: condition(id),
      level: gateLevel(seen !== undefined, clear !== undefined),
      open: clear ? progress(frame, clear.startFrame, slow) : 0,
      appear: appearOf(focus),
    };
    const owner = focus.event.session;
    const gone = hasEnded(owner);
    registrant = {
      label: sessionLabel(owner),
      sublabel: gone ? "ended · nothing polls" : "registered a gate",
      level: gone ? "done" : "running",
      // Re-enters when the focus moves to this gate's registration.
      appear: appearOf(focus),
    };
    edgeIn = progress(frame, focus.startFrame, slow);
    const next = started.find((b) => b.event.continues === id);
    if (next) {
      const done = hasEnded(next.event.session);
      continuation = {
        label: sessionLabel(next.event.session),
        sublabel: done ? "done" : "continues the work",
        level: done ? "done" : "running",
        appear: appearOf(next),
      };
    }
    if (clear) edgeOut = progress(frame, clear.startFrame, slow);
    waitedMs = Math.max(0, (clear ? clear.atMs : nowMs) - focus.atMs);
  } else {
    const first = started[0];
    if (first) {
      registrant = {
        label: sessionLabel(first.event.session),
        sublabel: hasEnded(first.event.session) ? "ended" : "working",
        level: hasEnded(first.event.session) ? "done" : "running",
        appear: appearOf(first),
      };
    }
  }

  // --- the gates panel ---------------------------------------------------------------
  const gates = registered.slice(-GATE_ROWS).map((b) => {
    const id = b.event.gate;
    const seen = observed.find((o) => o.event.gate === id);
    const clear = cleared.find((c) => c.event.gate === id);
    const next = started.find((s) => s.event.continues === id);
    const detail = clear
      ? `cleared · ${next ? `continued by ${sessionLabel(next.event.session)}` : "continuation dispatched"}`
      : seen
        ? "condition observed · clearing"
        : `waiting since ${formatDateTime(b.atMs)} · registered by ${sessionLabel(b.event.session)}`;
    return { id, text: condition(id), detail, level: gateLevel(seen !== undefined, clear !== undefined), appear: appearOf(b) };
  });

  // --- sessions on the real-time axis ---------------------------------------------------
  const startOf = (session: string) => trace.events.find((e) => e.kind === "session.started" && e.session === session);
  const endOf = (session: string) => trace.events.find((e) => e.kind === "session.ended" && e.session === session);
  // Started / ended follow the beats (what the nodes show); a session with no
  // start event counts as started from the beginning of the axis.
  let running = 0;
  const lanes = trace.entities.sessions.map((session) => {
    const s = startOf(session.id);
    const e = endOf(session.id);
    const isStarted = !s || started.some((b) => b.event.id === s.id);
    const isOver = e !== undefined && ended.some((b) => b.event.id === e.id);
    const startMs = s ? eventTimeMs(s) : firstMs;
    const endMs = e ? eventTimeMs(e) : Number.POSITIVE_INFINITY;
    if (!isStarted) return { id: session.id, label: session.label, start: 0, end: 0, level: "inert" as AttentionLevel };
    const over = isOver;
    if (!over) running++;
    return {
      id: session.id,
      label: session.label,
      start: axis(startMs),
      end: axis(Math.min(nowMs, endMs)),
      level: (over ? "done" : "running") as AttentionLevel,
    };
  });

  const tickLevel = (kind: S3Kind): AttentionLevel =>
    kind === "gate.registered"
      ? "waiting"
      : kind === "gate.observed"
        ? "testing"
        : kind === "gate.cleared" || kind === "session.ended"
          ? "done"
          : "running";
  const ticks = beats.map((b) => ({
    at: axis(b.atMs),
    level: tickLevel(b.event.kind as S3Kind),
    appear: progress(frame, b.startFrame, fast),
  }));

  // --- caption ---------------------------------------------------------------------------
  let caption: S3State["caption"] = null;
  if (current) {
    const event = current.event;
    const appear = appearOf(current);
    const text = (title: string, body: string) => ({ title, body: event.note ?? body, appear });
    if (event.kind === "session.started") {
      caption = event.continues
        ? text("A new session picks up the work", "It starts from what the gate recorded, not from scratch.")
        : text("A session takes the work", "It works until it reaches something it cannot finish yet.");
    } else if (event.kind === "gate.registered") {
      caption = text("Blocked, so it registers a gate", `Condition: ${condition(event.gate)}. Coord holds it, not the session.`);
    } else if (event.kind === "session.ended") {
      const waiting = registered.some((b) => b.event.session === event.session && !cleared.some((c) => c.event.gate === b.event.gate));
      caption = waiting
        ? text("The session ends", "Nothing runs or polls while the condition is false.")
        : text("The session ends", "Its part of the work is done.");
    } else if (event.kind === "gate.observed") {
      caption = text("The condition is observed", `Coord sees it in the twin: ${condition(event.gate)}.`);
    } else if (event.kind === "gate.cleared") {
      caption = text("The gate clears", "Coord dispatches the continuation.");
    }
  }

  return {
    title: trace.meta.title,
    gate,
    registrant,
    continuation,
    edgeIn,
    edgeOut,
    gates,
    running,
    waitedMs,
    lanes,
    ticks,
    playhead: axis(nowMs),
    range: { fromMs: firstMs, toMs: lastMs },
    caption,
  };
}
