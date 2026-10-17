/**
 * S6 "Self-correction" — the pure model. CI goes red; the session diagnoses
 * the failure, pushes a fix, and CI runs again, round the loop until it is
 * green, with no human step.
 *
 * Rendering lives in `SelfCorrection.tsx`.
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

/** The event kinds that drive S6 (its beats). */
export const S6_KINDS = [
  "pr.opened",
  "ci.started",
  "ci.failed",
  "ci.diagnosed",
  "fix.pushed",
  "ci.passed",
  "pr.landed",
] as const satisfies readonly EventKind[];

type S6Kind = (typeof S6_KINDS)[number];

export const S6_TIMING = {
  introMs: 1200,
  beatMs: 1100,
  outroMs: 2200,
} as const;

/** The loop, in order; "green" is where it leaves the loop. */
export const STEPS = [
  { id: "run", label: "CI runs" },
  { id: "red", label: "CI red" },
  { id: "diagnosed", label: "Diagnosed" },
  { id: "fixed", label: "Fix pushed" },
  { id: "green", label: "CI green" },
] as const;

type StepId = (typeof STEPS)[number]["id"];

const STEP_OF: Partial<Record<S6Kind, StepId>> = {
  "ci.started": "run",
  "ci.failed": "red",
  "ci.diagnosed": "diagnosed",
  "fix.pushed": "fixed",
  "ci.passed": "green",
  "pr.landed": "green",
};

const LEVEL_OF: Record<StepId, AttentionLevel> = {
  run: "testing",
  red: "attention",
  diagnosed: "running",
  fixed: "running",
  green: "done",
};

/** Rows of room in the session log. */
export const LOG_ROWS = 4;

export function s6Timeline(trace: SceneTrace, fps: number): Timeline {
  return buildTimeline(trace, { fps, ...S6_TIMING, kinds: S6_KINDS });
}

export interface StepState {
  id: StepId;
  label: string;
  level: AttentionLevel;
  emphasis: number;
}

export interface S6State {
  title: string;
  steps: StepState[];
  /** 0..1 the edge INTO each step (by step id) is drawn. */
  edges: Record<StepId, number>;
  pr: { repo: string; number: number; title: string; status: string; level: AttentionLevel; appear: number } | null;
  log: { id: string; text: string; detail: string; level: AttentionLevel; appear: number }[];
  /** CI runs so far. */
  runs: number;
  /** Real time from the first red run to green (or to now, while not green). */
  toGreenMs: number | null;
  /** Whether a pass has followed the first red run. */
  isGreen: boolean;
  caption: { title: string; body: string; appear: number } | null;
}

const PR_STATUS: Record<S6Kind, { text: string; level: AttentionLevel }> = {
  "pr.opened": { text: "Open", level: "running" },
  "ci.started": { text: "CI running", level: "testing" },
  "ci.failed": { text: "CI failed", level: "attention" },
  "ci.diagnosed": { text: "Diagnosed", level: "running" },
  "fix.pushed": { text: "Fix pushed", level: "running" },
  "ci.passed": { text: "CI passed", level: "done" },
  "pr.landed": { text: "Landed", level: "done" },
};

export function s6State(trace: SceneTrace, timeline: Timeline, frame: number): S6State {
  const fps = timeline.fps;
  const fast = msToFrames(durations.base, fps);
  const slow = msToFrames(durations.slow, fps);
  const beats = timeline.beats;
  const active = activeBeatIndex(timeline, frame);
  const passed = beats.slice(0, active + 1);
  const current = passed[passed.length - 1];
  const appearOf = (beat: Beat) => progress(frame, beat.startFrame, fast, easings.enter);

  // --- the loop: every step reached so far stays lit; the current one leads ------------
  const history = passed;
  const currentStep = current ? STEP_OF[current.event.kind as S6Kind] : undefined;
  const reached = new Set(history.map((b) => STEP_OF[b.event.kind as S6Kind]).filter((s): s is StepId => s !== undefined));
  const steps: StepState[] = STEPS.map((step) => {
    if (step.id === currentStep && current) {
      return { id: step.id, label: step.label, level: LEVEL_OF[step.id], emphasis: progress(frame, current.startFrame, fast) };
    }
    return { id: step.id, label: step.label, level: reached.has(step.id) ? "done" : "inert", emphasis: 0 };
  });
  const edgeInto = (id: StepId) => {
    const into = [...history].reverse().find((b) => STEP_OF[b.event.kind as S6Kind] === id);
    return into ? progress(frame, into.startFrame, slow) : 0;
  };
  const edges = Object.fromEntries(STEPS.map((s) => [s.id, edgeInto(s.id)])) as Record<StepId, number>;

  // --- the PR -----------------------------------------------------------------------------
  const prBeats = passed.filter((b) => "pr" in b.event);
  const lastPr = prBeats[prBeats.length - 1];
  let pr: S6State["pr"] = null;
  if (lastPr && "pr" in lastPr.event) {
    const entity = trace.entities.prs.find((p) => "pr" in lastPr.event && p.id === lastPr.event.pr);
    const status = PR_STATUS[lastPr.event.kind as S6Kind];
    if (entity) {
      pr = {
        repo: entity.repo,
        number: entity.number,
        title: entity.title,
        status: status.text,
        level: status.level,
        appear: appearOf(prBeats[0] as Beat),
      };
    }
  }

  // --- the session's log -----------------------------------------------------------------
  let run = 0;
  const log: S6State["log"] = [];
  for (const b of passed) {
    const e = b.event;
    if (e.kind === "ci.started") run++;
    else if (e.kind === "ci.failed") {
      const checks = e.checks !== undefined ? ` · ${e.checks} checks` : "";
      log.push({ id: e.id, text: `Run ${run} red${checks}`, detail: "CI reports the failure to the session", level: "attention", appear: appearOf(b) });
    } else if (e.kind === "ci.diagnosed") {
      log.push({ id: e.id, text: e.cause, detail: "diagnosis, from the CI log", level: "running", appear: appearOf(b) });
    } else if (e.kind === "fix.pushed") {
      log.push({ id: e.id, text: e.summary, detail: "fix pushed to the PR", level: "running", appear: appearOf(b) });
    } else if (e.kind === "ci.passed") {
      const checks = e.checks !== undefined ? ` · ${e.checks} checks` : "";
      log.push({ id: e.id, text: `Run ${run} green${checks}`, detail: "no human step", level: "done", appear: appearOf(b) });
    }
  }

  // --- time to green ---------------------------------------------------------------------
  const firstRed = passed.find((b) => b.event.kind === "ci.failed");
  const green = passed.find((b) => b.event.kind === "ci.passed" && firstRed !== undefined && b.index > firstRed.index);
  const nowMs = current ? current.atMs : 0;
  const toGreenMs = firstRed ? (green ? green.atMs : nowMs) - firstRed.atMs : null;

  // --- caption -----------------------------------------------------------------------------
  let caption: S6State["caption"] = null;
  if (current) {
    const e = current.event;
    const appear = appearOf(current);
    const text = (title: string, body: string) => ({ title, body: e.note ?? body, appear });
    if (e.kind === "pr.opened") caption = text("Pull request opened", "The change goes up for CI, as every change does.");
    else if (e.kind === "ci.started") caption = text(run > 1 ? "CI runs again" : "CI runs", run > 1 ? "On the fix, automatically." : "Every required check runs.");
    else if (e.kind === "ci.failed") caption = text("CI goes red", "Nobody is paged. The session that owns the PR reads the failure.");
    else if (e.kind === "ci.diagnosed") caption = text("Diagnosed", `From the CI log: ${e.cause}.`);
    else if (e.kind === "fix.pushed") caption = text("Fixed", `The session pushes the fix: ${e.summary}.`);
    else if (e.kind === "ci.passed") caption = text("Green", "Every check passes. No human stepped in.");
    else if (e.kind === "pr.landed") caption = text("Landed", "The corrected change is on main.");
  }

  return {
    title: trace.meta.title,
    steps,
    edges,
    pr,
    log: log.slice(-LOG_ROWS),
    runs: run,
    toGreenMs,
    isGreen: green !== undefined,
    caption,
  };
}

