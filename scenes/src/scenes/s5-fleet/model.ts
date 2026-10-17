/**
 * S5 "The fleet" — the pure model. Agent sessions run on several machines,
 * each in its own isolated worktree, and coordinate through coord: they
 * declare their intent and claim what they will edit, and a session that
 * finds a claim already held picks other work instead of colliding.
 *
 * Rendering lives in `Fleet.tsx`.
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

/** The event kinds that drive S5 (its beats). */
export const S5_KINDS = [
  "session.started",
  "session.ended",
  "worktree.allocated",
  "intent.declared",
  "claim.acquired",
  "claim.contended",
  "claim.released",
] as const satisfies readonly EventKind[];

type S5Kind = (typeof S5_KINDS)[number];

export const S5_TIMING = {
  introMs: 1200,
  beatMs: 900,
  outroMs: 2200,
} as const;

export const MAX_MACHINES = 3;
export const SESSIONS_PER_MACHINE = 2;
/** Rows of room in the coordination log. */
export const LOG_ROWS = 5;

export function s5Timeline(trace: SceneTrace, fps: number): Timeline {
  return buildTimeline(trace, { fps, ...S5_TIMING, kinds: S5_KINDS });
}

export interface Row {
  id: string;
  text: string;
  detail: string;
  level: AttentionLevel;
  appear: number;
}

export interface S5State {
  title: string;
  machines: { id: string; label: string; sessions: Row[] }[];
  /** Coordination through coord, oldest first (at most LOG_ROWS). */
  log: Row[];
  running: number;
  /** Claims that were already held, so a collision did not happen. */
  avoided: number;
  caption: { title: string; body: string; appear: number } | null;
}

function isS5Kind(kind: EventKind): kind is S5Kind {
  return (S5_KINDS as readonly EventKind[]).includes(kind);
}

export function s5State(trace: SceneTrace, timeline: Timeline, frame: number): S5State {
  const fast = msToFrames(durations.base, timeline.fps);
  const beats = timeline.beats;
  const active = activeBeatIndex(timeline, frame);
  const passed = beats.slice(0, active + 1);
  const current = passed[passed.length - 1];
  const appearOf = (beat: Beat) => progress(frame, beat.startFrame, fast, easings.enter);
  const sessionLabel = (id: string | undefined) => trace.entities.sessions.find((s) => s.id === id)?.label ?? id ?? "a session";
  const machineLabel = (id: string | undefined) => trace.entities.machines.find((m) => m.id === id)?.label ?? "a machine";

  // --- each session's card on its machine ---------------------------------------------
  const card = (sessionId: string, label: string): Row | null => {
    const mine = passed.filter((b) => "session" in b.event && b.event.session === sessionId && isS5Kind(b.event.kind));
    const start = mine.find((b) => b.event.kind === "session.started");
    if (!start) return null;
    const worktree = [...mine].reverse().find((b) => b.event.kind === "worktree.allocated");
    const last = mine[mine.length - 1] as Beat;
    const e = last.event;
    let detail = "starting";
    let level: AttentionLevel = "running";
    if (e.kind === "worktree.allocated") detail = "in its own worktree";
    else if (e.kind === "intent.declared") detail = `intent: ${e.intent}`;
    else if (e.kind === "claim.acquired") detail = `holds ${e.resource}`;
    else if (e.kind === "claim.contended") {
      detail = `${e.resource} is taken · picks other work`;
      level = "waiting";
    } else if (e.kind === "claim.released") detail = `released ${e.resource}`;
    else if (e.kind === "session.ended") {
      detail = "done";
      level = "done";
    }
    const repo = worktree && worktree.event.kind === "worktree.allocated" ? worktree.event.repo : null;
    return {
      id: sessionId,
      text: repo ? `${label} · worktree of ${repo}` : label,
      detail,
      level,
      appear: appearOf(start),
    };
  };

  const machines = trace.entities.machines.slice(0, MAX_MACHINES).map((machine) => ({
    id: machine.id,
    label: machine.label,
    sessions: trace.entities.sessions
      .filter((s) => s.machine === machine.id)
      .slice(0, SESSIONS_PER_MACHINE)
      .map((s) => card(s.id, s.label))
      .filter((r): r is Row => r !== null),
  }));
  const running = machines.reduce((n, m) => n + m.sessions.filter((s) => s.level !== "done").length, 0);

  // --- the coordination log: intent and claims, as coord sees them ------------------------
  const log: Row[] = [];
  let avoided = 0;
  for (const b of passed) {
    const e = b.event;
    const who = "session" in e ? sessionLabel(e.session) : "";
    if (e.kind === "intent.declared") {
      log.push({ id: e.id, text: e.intent, detail: `intent declared by ${who}`, level: "landing", appear: appearOf(b) });
    } else if (e.kind === "claim.acquired") {
      log.push({ id: e.id, text: e.resource, detail: `claimed by ${who}`, level: "running", appear: appearOf(b) });
    } else if (e.kind === "claim.contended") {
      avoided++;
      log.push({
        id: e.id,
        text: e.resource,
        detail: `${who} sees it held by ${sessionLabel(e.holder)} · no collision`,
        level: "waiting",
        appear: appearOf(b),
      });
    } else if (e.kind === "claim.released") {
      log.push({ id: e.id, text: e.resource, detail: `released by ${who}`, level: "done", appear: appearOf(b) });
    }
  }

  // --- caption ---------------------------------------------------------------------------
  let caption: S5State["caption"] = null;
  if (current && "session" in current.event) {
    const e = current.event;
    const who = sessionLabel(e.session);
    const text = (title: string, body: string) => ({ title, body: e.note ?? body, appear: appearOf(current) });
    if (e.kind === "session.started") {
      const machine = trace.entities.sessions.find((s) => s.id === e.session)?.machine;
      caption = text("A session starts", `${who} starts on ${machineLabel(machine)}.`);
    } else if (e.kind === "worktree.allocated") {
      caption = text("Its own worktree", `${who} works in an isolated worktree of ${e.repo}; no session shares a checkout.`);
    } else if (e.kind === "intent.declared") {
      caption = text("Intent, declared", `${who} says what it is doing, and every session can see it in coord.`);
    } else if (e.kind === "claim.acquired") {
      caption = text("Claim before editing", `${who} claims ${e.resource} before it touches it.`);
    } else if (e.kind === "claim.contended") {
      caption = text("Already claimed", `${e.resource} is held by ${sessionLabel(e.holder)}, so ${who} picks other work instead of colliding.`);
    } else if (e.kind === "claim.released") {
      caption = text("Released", `${who} releases ${e.resource} once its work has landed.`);
    } else if (e.kind === "session.ended") {
      caption = text("Done", `${who} ends; its claims are released and its worktree is reclaimed.`);
    }
  }

  return { title: trace.meta.title, machines, log: log.slice(-LOG_ROWS), running, avoided, caption };
}
