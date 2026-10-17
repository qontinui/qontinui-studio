/**
 * S4 "The merge train" — the pure model. Several PRs are queued; the head of
 * the train is rebased onto main, tested on exactly that code, and landed,
 * in order. A rebase that conflicts takes its PR out of line so the train
 * keeps moving; once a session resolves it, the PR rejoins at the back.
 *
 * Rendering lives in `MergeTrain.tsx`.
 */
import {
  activeBeatIndex,
  buildTimeline,
  durations,
  easings,
  formatTime,
  mix,
  msToFrames,
  progress,
  type AttentionLevel,
  type Beat,
  type EventKind,
  type SceneTrace,
  type Timeline,
} from "../../api";

/** The event kinds that drive S4 (its beats). */
export const S4_KINDS = [
  "merge_train.queued",
  "merge_train.rebased",
  "merge_train.conflicted",
  "merge_train.resolved",
  "ci.started",
  "ci.passed",
  "ci.failed",
  "pr.landed",
] as const satisfies readonly EventKind[];

type S4Kind = (typeof S4_KINDS)[number];

export const S4_TIMING = {
  introMs: 1200,
  beatMs: 900,
  outroMs: 2200,
} as const;

/** Slots in the train (the scene's check caps the PR count at this). */
export const TRAIN_SLOTS = 4;

export function s4Timeline(trace: SceneTrace, fps: number): Timeline {
  return buildTimeline(trace, { fps, ...S4_TIMING, kinds: S4_KINDS });
}

const STATUS: Record<S4Kind, { text: string; level: AttentionLevel }> = {
  "merge_train.queued": { text: "Queued", level: "waiting" },
  "merge_train.rebased": { text: "Rebased · testing", level: "testing" },
  "merge_train.conflicted": { text: "Conflict", level: "attention" },
  "merge_train.resolved": { text: "Resolved · queued", level: "waiting" },
  "ci.started": { text: "Testing", level: "testing" },
  "ci.passed": { text: "Passed · landing", level: "landing" },
  "ci.failed": { text: "Tests failed", level: "attention" },
  "pr.landed": { text: "Landed", level: "done" },
};

/** Where each PR stands after the first `count` beats. */
interface Standing {
  /** Order key: the beat that put it in line (queued / resolved / conflicted). */
  key: number;
  /** Out of line: a conflict not yet resolved, or failed tests. */
  aside: boolean;
  landed: boolean;
  last: S4Kind;
}

function standings(beats: readonly Beat[], count: number): Map<string, Standing> {
  const out = new Map<string, Standing>();
  for (const beat of beats.slice(0, count)) {
    const event = beat.event;
    if (!("pr" in event)) continue;
    const kind = event.kind as S4Kind;
    const prev = out.get(event.pr);
    if (kind === "merge_train.queued" || kind === "merge_train.resolved") {
      out.set(event.pr, { key: beat.index, aside: false, landed: false, last: kind });
    } else if (kind === "merge_train.conflicted" || kind === "ci.failed") {
      out.set(event.pr, { key: beat.index, aside: true, landed: false, last: kind });
    } else if (kind === "ci.passed" && prev?.aside && prev.last !== "merge_train.conflicted") {
      // Green again after a red run: back in line, at the back.
      out.set(event.pr, { key: beat.index, aside: false, landed: false, last: kind });
    } else if (prev) {
      out.set(event.pr, { ...prev, landed: kind === "pr.landed", last: kind });
    }
  }
  return out;
}

/** Slot of every PR still in the train: in line by key, then those out of line. */
function slots(standing: Map<string, Standing>): Map<string, number> {
  const live = [...standing.entries()].filter(([, s]) => !s.landed);
  const order = [
    ...live.filter(([, s]) => !s.aside).sort((a, b) => a[1].key - b[1].key),
    ...live.filter(([, s]) => s.aside).sort((a, b) => a[1].key - b[1].key),
  ];
  return new Map(order.map(([id], i) => [id, i]));
}

export interface CarState {
  id: string;
  repo: string;
  number: number;
  title: string;
  status: string;
  level: AttentionLevel;
  /** Slot position (fractional while moving). */
  slot: number;
  /** 0..1 stepped out of line. */
  aside: number;
  /** 0..1 entrance. */
  appear: number;
  /** 0..1 leaving the train for main. */
  exit: number;
}

export interface S4State {
  title: string;
  cars: CarState[];
  /** Landed PRs, in landing order. */
  main: { id: string; text: string; detail: string; level: AttentionLevel; appear: number }[];
  landed: number;
  total: number;
  caption: { title: string; body: string; appear: number } | null;
}

export function s4State(trace: SceneTrace, timeline: Timeline, frame: number): S4State {
  const fps = timeline.fps;
  const fast = msToFrames(durations.base, fps);
  const slow = msToFrames(durations.slow, fps);
  const beats = timeline.beats;
  const active = activeBeatIndex(timeline, frame);
  const current = beats[active];
  const now = standings(beats, active + 1);
  const before = standings(beats, Math.max(0, active));
  const slotNow = slots(now);
  const slotBefore = slots(before);
  // On a landing beat the landed car leaves first (over `fast`) and the rest
  // move up after it (over `slow`); fast + slow fits inside one beat.
  const landingBeat = current?.event.kind === "pr.landed";
  const moveStart = current ? current.startFrame + (landingBeat ? fast : 0) : 0;
  // ...and settles before the next beat starts.
  const moveFrames = landingBeat ? Math.max(1, Math.min(slow, timeline.beatFrames - fast - 1)) : slow;
  const t = current ? progress(frame, moveStart, moveFrames, easings.standard) : 1;
  const prById = new Map(trace.entities.prs.map((p) => [p.id, p]));

  const cars: CarState[] = [];
  for (const [id, s] of now) {
    const pr = prById.get(id);
    if (!pr) continue;
    const landingNow = s.landed && current !== undefined && "pr" in current.event && current.event.pr === id && current.event.kind === "pr.landed";
    if (s.landed && !landingNow) continue;
    const firstBeat = beats.find((b) => "pr" in b.event && b.event.pr === id);
    const target = landingNow ? (slotBefore.get(id) ?? 0) : (slotNow.get(id) ?? 0);
    const from = slotBefore.get(id) ?? target;
    const wasAside = before.get(id)?.aside ?? false;
    const exit = landingNow && current ? progress(frame, current.startFrame, fast, easings.exit) : 0;
    // A landed PR leaves the train; once it is gone it is drawn in main only.
    if (exit >= 1) continue;
    cars.push({
      id,
      repo: pr.repo,
      number: pr.number,
      title: pr.title,
      status: STATUS[s.last].text,
      level: STATUS[s.last].level,
      slot: mix(from, target, t),
      aside: mix(wasAside ? 1 : 0, s.aside ? 1 : 0, t),
      appear: firstBeat ? progress(frame, firstBeat.startFrame, fast, easings.enter) : 1,
      exit,
    });
  }
  // By slot, with a leaving car drawn last so nothing slides over it.
  cars.sort((a, b) => (a.exit > 0 ? 1 : 0) - (b.exit > 0 ? 1 : 0) || a.slot - b.slot);

  const landedBeats = beats.slice(0, active + 1).filter((b) => b.event.kind === "pr.landed");
  const main = landedBeats.map((b) => {
    const pr = "pr" in b.event ? prById.get(b.event.pr) : undefined;
    return {
      id: b.event.id,
      text: pr ? `#${pr.number} ${pr.title}` : b.event.id,
      detail: `landed ${formatTime(b.atMs)} · tested on this main`,
      level: "done" as AttentionLevel,
      appear: progress(frame, b.startFrame, fast, easings.enter),
    };
  });

  let caption: S4State["caption"] = null;
  if (current && "pr" in current.event) {
    const event = current.event;
    const pr = prById.get(event.pr);
    const name = pr ? `#${pr.number}` : event.pr;
    const appear = progress(frame, current.startFrame, fast, easings.enter);
    const text = (title: string, body: string) => ({ title, body: event.note ?? body, appear });
    switch (event.kind as S4Kind) {
      case "merge_train.queued":
        caption = text("Queued", `${name} joins the train. Coord orders it; nobody merges by hand.`);
        break;
      case "merge_train.rebased":
        caption = text("Rebased onto main", `${name} is at the head of the train, rebased onto the main it will land on.`);
        break;
      case "ci.started":
        caption = text("Tested", `${name} runs its checks on the rebased code.`);
        break;
      case "ci.passed":
        caption = text("Green on what will land", `${name} passed on exactly the code that lands, not on an old base.`);
        break;
      case "ci.failed":
        caption = text("Tests fail", `${name} steps out of line, so the rest of the train keeps moving.`);
        break;
      case "merge_train.conflicted":
        caption = text("A conflict", `${name} conflicts with what just landed. It steps out so the train keeps moving.`);
        break;
      case "merge_train.resolved":
        caption = text("Conflict resolved", `A session resolves ${name}; it rejoins the train at the back.`);
        break;
      case "pr.landed":
        caption = text("Landed, in order", `${name} is on main.`);
        break;
    }
  }

  return {
    title: trace.meta.title,
    cars,
    main,
    landed: landedBeats.length,
    total: trace.entities.prs.length,
    caption,
  };
}
