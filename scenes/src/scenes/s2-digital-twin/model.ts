/**
 * S2 "The digital twin" — the pure model. GitHub, CI, machines and sessions
 * are mirrored into coord; decisions are made from that twin, not from
 * guesses; and the developer twin answers an agent's question from served
 * policy and intent documents instead of waking the operator.
 *
 * Rendering lives in `DigitalTwin.tsx`; this file says what every part looks
 * like at a given frame, so it is unit-testable.
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
  type EventOfKind,
  type SceneTrace,
  type Timeline,
} from "../../api";

/** The event kinds that drive S2 (its beats). */
export const S2_KINDS = ["twin.mirrored", "twin.decided", "question.asked", "question.answered"] as const satisfies readonly EventKind[];

export const S2_TIMING = {
  introMs: 1200,
  beatMs: 1300,
  outroMs: 2200,
} as const;

/** Rows of room in each panel (the panels never grow). */
export const TWIN_ROWS = 5;
export const DECISION_ROWS = 3;
export const DEVELOPER_ROWS = 2;

export function s2Timeline(trace: SceneTrace, fps: number): Timeline {
  return buildTimeline(trace, { fps, ...S2_TIMING, kinds: S2_KINDS });
}

type SourceKind = SceneTrace["entities"]["sources"][number]["kind"];

const MIRROR_BODY: Record<SourceKind, string> = {
  "repo-host": "Pull requests and pushes on GitHub are mirrored into coord as they happen.",
  ci: "Check runs are mirrored as they finish, so the twin knows what is green.",
  machine: "Each machine reports its capacity and what it is running.",
  session: "Every agent session reports what it is working on.",
};

const ANSWER_TITLE: Record<EventOfKind<"question.answered">["from"], string> = {
  policy: "Answered from served policy",
  intent: "Answered from declared intent",
  operator: "The operator answers",
};

export interface SourceState {
  id: string;
  label: string;
  /** Inert until the source first reports; running while it reports; then done. */
  level: AttentionLevel;
  emphasis: number;
  /** 0..1 the edge into the twin is drawn (from the source's first report on). */
  pulse: number;
}

export interface RowState {
  id: string;
  text: string;
  detail?: string;
  level: AttentionLevel;
  appear: number;
}

export interface S2State {
  title: string;
  sources: SourceState[];
  /** The latest facts in the twin, oldest first (at most TWIN_ROWS). */
  twin: RowState[];
  /** How many facts the twin holds now. */
  factCount: number;
  decisions: RowState[];
  /** 0..1 the twin → decisions edge, drawn at the first decision and kept. */
  decisionPulse: number;
  developer: RowState[];
  /** 0..1 the twin → developer-twin edge, drawn at the first question and kept. */
  developerPulse: number;
  /** `short` is for layouts with no room for the full sublabel. */
  operator: { level: AttentionLevel; sublabel: string; short: string };
  caption: { title: string; body: string; appear: number } | null;
}

/** `text` as a sentence: a full stop added unless it already ends in punctuation. */
function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/** Beats of `kind` that have started at `frame`. */
function started<K extends (typeof S2_KINDS)[number]>(beats: readonly Beat[], kind: K): (Beat & { event: EventOfKind<K> })[] {
  return beats.filter((b): b is Beat & { event: EventOfKind<K> } => b.event.kind === kind);
}

/** Level of the newest item while its beat runs, then done. */
function freshLevel(beat: Beat, frame: number, beatFrames: number, fresh: AttentionLevel): AttentionLevel {
  return frame < beat.startFrame + beatFrames ? fresh : "done";
}

export function s2State(trace: SceneTrace, timeline: Timeline, frame: number): S2State {
  const fps = timeline.fps;
  const fast = msToFrames(durations.base, fps);
  const slow = msToFrames(durations.slow, fps);
  const { beatFrames } = timeline;
  const active = activeBeatIndex(timeline, frame);
  const passed = timeline.beats.slice(0, active + 1);
  const current = passed[passed.length - 1];
  const appearOf = (beat: Beat) => progress(frame, beat.startFrame, fast, easings.enter);
  const pulseOf = (beat: Beat | undefined) => (beat ? progress(frame, beat.startFrame, slow, easings.standard) : 0);

  // --- sources and their edges into the twin ---------------------------------
  const mirrored = started(passed, "twin.mirrored");
  const sourceLabel = new Map(trace.entities.sources.map((s) => [s.id, s.label]));
  const sources: SourceState[] = trace.entities.sources.map((source) => {
    const reports = mirrored.filter((b) => b.event.source === source.id);
    const last = reports[reports.length - 1];
    if (!last) return { id: source.id, label: source.label, level: "inert", emphasis: 0, pulse: 0 };
    const reporting = frame < last.startFrame + beatFrames;
    const end = last.startFrame + beatFrames;
    return {
      id: source.id,
      label: source.label,
      level: reporting ? "running" : "done",
      // Rises at the report, eases out once its beat is over.
      emphasis: reporting ? progress(frame, last.startFrame, fast) : 1 - progress(frame, end, fast),
      // The edge draws at the source's first report and then stays drawn; a
      // repeat report shows as the node lighting up again, never a blank edge.
      pulse: pulseOf(reports[0]),
    };
  });

  // --- the twin: the latest facts ----------------------------------------------
  const twin: RowState[] = mirrored.slice(-TWIN_ROWS).map((b) => ({
    id: b.event.id,
    text: b.event.fact,
    detail: `from ${sourceLabel.get(b.event.source) ?? b.event.source}`,
    level: freshLevel(b, frame, beatFrames, "running"),
    appear: appearOf(b),
  }));

  // --- decisions made from the twin -----------------------------------------------
  const decided = started(passed, "twin.decided");
  const decisions: RowState[] = decided.slice(-DECISION_ROWS).map((b) => ({
    id: b.event.id,
    text: b.event.decision,
    detail: `from the twin: ${b.event.basis}`,
    level: freshLevel(b, frame, beatFrames, "running"),
    appear: appearOf(b),
  }));

  // --- the developer twin: questions and their answers ------------------------------
  const asked = started(passed, "question.asked");
  const answered = started(passed, "question.answered");
  const sessionLabel = new Map(trace.entities.sessions.map((s) => [s.id, s.label]));
  const qa: { beat: Beat; row: RowState }[] = [];
  for (const b of asked) {
    const answer = answered.find((a) => a.event.question === b.event.id);
    qa.push({
      beat: b,
      row: {
        id: b.event.id,
        text: b.event.question,
        detail: `asked by ${sessionLabel.get(b.event.session) ?? b.event.session}`,
        level: answer ? "done" : "waiting",
        appear: appearOf(b),
      },
    });
  }
  for (const a of answered) {
    qa.push({
      beat: a,
      row: {
        id: a.event.id,
        text: a.event.answer,
        detail: a.event.from === "operator" ? "from the operator" : `from served ${a.event.document ?? a.event.from}`,
        level: a.event.from === "operator" ? "attention" : "done",
        appear: appearOf(a),
      },
    });
  }
  qa.sort((x, y) => x.beat.startFrame - y.beat.startFrame);
  const developer = qa.slice(-DEVELOPER_ROWS).map((x) => x.row);
  const questionBeats = [...asked, ...answered].sort((x, y) => x.startFrame - y.startFrame);

  // --- the operator: woken only if an answer had to come from them -------------------
  const fromDocuments = answered.filter((a) => a.event.from !== "operator").length;
  const woken = answered.some((a) => a.event.from === "operator");
  const operator = {
    level: (woken ? "attention" : "inert") as AttentionLevel,
    sublabel: woken
      ? "woken to answer"
      : fromDocuments === 0
        ? "not woken"
        : `not woken · ${fromDocuments} ${fromDocuments === 1 ? "answer" : "answers"} from documents`,
    short: woken ? "woken" : "not woken",
  };

  // --- caption -----------------------------------------------------------------------
  let caption: S2State["caption"] = null;
  if (current) {
    const event = current.event;
    const appear = appearOf(current);
    if (event.kind === "twin.mirrored") {
      const source = trace.entities.sources.find((s) => s.id === event.source);
      caption = {
        title: "Mirrored into the twin",
        body: event.note ?? (source ? MIRROR_BODY[source.kind] : "A fact is mirrored into coord."),
        appear,
      };
    } else if (event.kind === "twin.decided") {
      caption = { title: "Decided from the twin, not guessed", body: event.note ?? sentence(`${event.decision}: ${event.basis}`), appear };
    } else if (event.kind === "question.asked") {
      caption = { title: "A session has a question", body: event.note ?? `"${event.question}"`, appear };
    } else if (event.kind === "question.answered") {
      caption = {
        title: ANSWER_TITLE[event.from],
        body:
          event.note ??
          (event.from === "operator"
            ? event.answer
            : `${sentence(event.answer)} Cited: ${event.document ?? event.from}. The operator is not woken.`),
        appear,
      };
    }
  }

  return {
    title: trace.meta.title,
    sources,
    twin,
    factCount: mirrored.length,
    decisions,
    decisionPulse: pulseOf(decided[0]),
    developer,
    developerPulse: pulseOf(questionBeats[0]),
    operator,
    caption,
  };
}
