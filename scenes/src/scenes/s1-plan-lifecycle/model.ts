/**
 * S1 "Plan lifecycle" — the pure model: what each stage, the PR, the gate and
 * the session lanes look like at a given frame. Rendering lives in
 * `PlanLifecycle.tsx`; keeping the model separate keeps it unit-testable.
 */
import {
  activeBeatIndex,
  buildTimeline,
  durations,
  easings,
  eventTimeMs,
  msToFrames,
  progress,
  realTimeAt,
  type AttentionLevel,
  type Beat,
  type EventKind,
  type SceneTrace,
  type Timeline,
} from "../../api";

export const STAGES = [
  { id: "draft", label: "Draft" },
  { id: "vetted", label: "Vetted" },
  { id: "implementing", label: "Implementing" },
  { id: "pr", label: "PR opened" },
  { id: "ci", label: "CI" },
  { id: "train", label: "Merge train" },
  { id: "landed", label: "Landed" },
  { id: "shipped", label: "Shipped" },
] as const;

/** The event kinds that move the plan between stages (the scene's beats). */
export const STAGE_KINDS = [
  "plan.drafted",
  "plan.vetted",
  "plan.implementing",
  "pr.opened",
  "ci.started",
  "ci.failed",
  "ci.passed",
  "merge_train.queued",
  "pr.landed",
  "plan.shipped",
] as const satisfies readonly EventKind[];

type StageKind = (typeof STAGE_KINDS)[number];

const STAGE_OF: Record<StageKind, number> = {
  "plan.drafted": 0,
  "plan.vetted": 1,
  "plan.implementing": 2,
  "pr.opened": 3,
  "ci.started": 4,
  "ci.failed": 4,
  "ci.passed": 4,
  "merge_train.queued": 5,
  "pr.landed": 6,
  "plan.shipped": 7,
};

/**
 * Who must act while the plan sits at the stage this event moved it to —
 * the attention-layer rule (colour encodes who must act, not alarm).
 */
const LEVEL_OF: Record<StageKind, AttentionLevel> = {
  "plan.drafted": "waiting",
  "plan.vetted": "waiting",
  "plan.implementing": "running",
  "pr.opened": "running",
  "ci.started": "testing",
  "ci.failed": "attention",
  "ci.passed": "done",
  "merge_train.queued": "landing",
  "pr.landed": "landing",
  "plan.shipped": "done",
};

/**
 * The PR's own level differs from the plan stage's in one place: a landed PR
 * is finished ("done") while the plan it belongs to is still on its way to
 * shipped ("landing").
 */
function prLevel(kind: StageKind): AttentionLevel {
  return kind === "pr.landed" ? "done" : LEVEL_OF[kind];
}

const PR_STATUS: Partial<Record<StageKind, string>> = {
  "pr.opened": "Open",
  "ci.started": "CI running",
  "ci.failed": "CI failed",
  "ci.passed": "CI passed",
  "merge_train.queued": "In merge train",
  "pr.landed": "Landed",
  "plan.shipped": "Landed",
};

const CAPTIONS: Record<StageKind, { title: string; body: string }> = {
  "plan.drafted": { title: "A plan is drafted", body: "The work starts as a written plan, not a prompt." },
  "plan.vetted": { title: "Vetted", body: "An independent reviewer checks it against the repos and policy." },
  "plan.implementing": { title: "Implementing", body: "An agent session takes the plan in an isolated worktree." },
  "pr.opened": { title: "Pull request opened", body: "The change is proposed, never pushed straight to main." },
  "ci.started": { title: "CI runs", body: "Checks gate the change before anyone merges it." },
  "ci.failed": { title: "CI fails", body: "A red check needs action, so the session fixes it." },
  "ci.passed": { title: "CI passes", body: "Every required check is green." },
  "merge_train.queued": { title: "Into the merge train", body: "Coord queues it, rebases and re-tests in order." },
  "pr.landed": { title: "Landed", body: "The change is on main." },
  "plan.shipped": { title: "Shipped", body: "Every phase landed: the plan is done." },
};

export const S1_TIMING = {
  introMs: 1200,
  beatMs: 1000,
  outroMs: 2000,
} as const;

export function s1Timeline(trace: SceneTrace, fps: number): Timeline {
  return buildTimeline(trace, { fps, ...S1_TIMING, kinds: STAGE_KINDS });
}

function isStageKind(kind: EventKind): kind is StageKind {
  return (STAGE_KINDS as readonly EventKind[]).includes(kind);
}

export interface StageState {
  label: string;
  level: AttentionLevel;
  /** 0..1 entrance at the start of the scene. */
  appear: number;
  /** 0..1 highlight while it is the current stage. */
  emphasis: number;
}

export interface EdgeState {
  progress: number;
  level: AttentionLevel;
}

export interface LaneState {
  /** Session id — the React key; labels need not be unique. */
  id: string;
  label: string;
  start: number;
  end: number;
  level: AttentionLevel;
}

export interface S1State {
  stages: StageState[];
  edges: EdgeState[];
  caption: { title: string; body: string; appear: number } | null;
  pr: { repo: string; number: number; title: string; status: string; level: AttentionLevel; appear: number } | null;
  gate: { open: number; level: AttentionLevel; label: string; appear: number } | null;
  lanes: LaneState[];
  ticks: { at: number; level: AttentionLevel; appear: number }[];
  /** Playhead on the real-time axis, 0..1. */
  playhead: number;
  /** Real elapsed ms since the first beat. */
  elapsedMs: number;
  range: { fromMs: number; toMs: number };
  plan: { title: string };
}

export function s1State(trace: SceneTrace, timeline: Timeline, frame: number): S1State {
  const fps = timeline.fps;
  const fast = msToFrames(durations.base, fps);
  const slow = msToFrames(durations.slow, fps);
  const beats = timeline.beats;
  const active = activeBeatIndex(timeline, frame);
  const passed = beats.slice(0, active + 1);
  const current: Beat | undefined = passed[passed.length - 1];

  const firstMs = beats.length ? (beats[0] as Beat).atMs : 0;
  const lastMs = beats.length ? (beats[beats.length - 1] as Beat).atMs : 0;
  const span = Math.max(1, lastMs - firstMs);
  const nowMs = realTimeAt(timeline, frame);
  const axis = (ms: number) => Math.min(1, Math.max(0, (ms - firstMs) / span));

  // --- stages -------------------------------------------------------------
  const currentStage = current && isStageKind(current.event.kind) ? STAGE_OF[current.event.kind] : -1;
  const currentLevel: AttentionLevel =
    current && isStageKind(current.event.kind) ? LEVEL_OF[current.event.kind] : "inert";
  const firstReach = STAGES.map((_, i) =>
    beats.find((b) => isStageKind(b.event.kind) && STAGE_OF[b.event.kind] === i),
  );
  const stages: StageState[] = STAGES.map((stage, i) => {
    const appear = progress(frame, i * 3, fast, easings.enter);
    if (i < currentStage) return { label: stage.label, level: "done", appear, emphasis: 0 };
    if (i === currentStage && current) {
      return {
        label: stage.label,
        level: currentLevel,
        appear,
        emphasis: progress(frame, current.startFrame, fast),
      };
    }
    return { label: stage.label, level: "inert", appear, emphasis: 0 };
  });

  // --- edges: drawn as the plan reaches the next stage --------------------
  const edges: EdgeState[] = STAGES.slice(1).map((_, i) => {
    const reach = firstReach[i + 1];
    if (!reach) return { progress: 0, level: "inert" };
    return { progress: progress(frame, reach.startFrame, fast), level: "done" };
  });

  // --- caption --------------------------------------------------------------
  const caption =
    current && isStageKind(current.event.kind)
      ? {
          title: CAPTIONS[current.event.kind].title,
          body: current.event.note ?? CAPTIONS[current.event.kind].body,
          appear: progress(frame, current.startFrame, fast, easings.enter),
        }
      : null;

  // --- PR card and the CI gate ---------------------------------------------
  const prEvents = passed.filter((b) => "pr" in b.event);
  const lastPrBeat = prEvents[prEvents.length - 1];
  let pr: S1State["pr"] = null;
  let gate: S1State["gate"] = null;
  if (lastPrBeat && "pr" in lastPrBeat.event) {
    const prId = lastPrBeat.event.pr;
    const entity = trace.entities.prs.find((p) => p.id === prId);
    const opened = prEvents.find((b) => b.event.kind === "pr.opened") ?? lastPrBeat;
    const kind = lastPrBeat.event.kind as StageKind;
    if (entity) {
      pr = {
        repo: entity.repo,
        number: entity.number,
        title: entity.title,
        status: PR_STATUS[kind] ?? "Open",
        level: prLevel(kind),
        appear: progress(frame, opened.startFrame, slow, easings.enter),
      };
    }
    const ciBeats = prEvents.filter((b) => b.event.kind.startsWith("ci."));
    const lastCi = ciBeats[ciBeats.length - 1];
    if (lastCi) {
      const passedCi = lastCi.event.kind === "ci.passed";
      const failedCi = lastCi.event.kind === "ci.failed";
      const checks = "checks" in lastCi.event ? lastCi.event.checks : undefined;
      gate = {
        open: passedCi ? progress(frame, lastCi.startFrame, slow) : 0,
        level: LEVEL_OF[lastCi.event.kind as StageKind],
        label: passedCi
          ? `CI open${checks !== undefined ? ` · ${checks} checks` : ""}`
          : failedCi
            ? "CI blocked"
            : "CI checking",
        appear: progress(frame, (ciBeats[0] as Beat).startFrame, fast),
      };
    }
  }

  // --- session lanes on the real-time axis -----------------------------------
  const lanes: LaneState[] = trace.entities.sessions.map((session) => {
    const startEv = trace.events.find((e) => e.kind === "session.started" && e.session === session.id);
    const endEv = trace.events.find((e) => e.kind === "session.ended" && e.session === session.id);
    const startMs = startEv ? eventTimeMs(startEv) : Number.POSITIVE_INFINITY;
    const endMs = endEv ? eventTimeMs(endEv) : Number.POSITIVE_INFINITY;
    if (nowMs < startMs) return { id: session.id, label: session.label, start: 0, end: 0, level: "inert" };
    const ended = nowMs >= endMs;
    return {
      id: session.id,
      label: session.label,
      start: axis(startMs),
      end: axis(Math.min(nowMs, endMs)),
      level: ended ? "done" : "running",
    };
  });

  // --- timeline ticks --------------------------------------------------------
  const ticks = beats.map((b) => ({
    at: axis(b.atMs),
    level: isStageKind(b.event.kind) ? LEVEL_OF[b.event.kind] : ("inert" as AttentionLevel),
    appear: progress(frame, b.startFrame, fast),
  }));

  const plan = trace.entities.plans[0];
  return {
    stages,
    edges,
    caption,
    pr,
    gate,
    lanes,
    ticks,
    playhead: axis(nowMs),
    elapsedMs: Math.max(0, nowMs - firstMs),
    range: { fromMs: firstMs, toMs: lastMs },
    plan: { title: plan ? plan.title : trace.meta.title },
  };
}
