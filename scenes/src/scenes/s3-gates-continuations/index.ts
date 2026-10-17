import { defineScene } from "../../api";
import { GatesContinuations } from "./GatesContinuations";
import { s3Timeline } from "./model";

export const s3GatesContinuations = defineScene({
  id: "s3-gates-continuations",
  title: "Gates and continuations",
  requires: {
    // Illustrative only: the build-record export (build-record/1) carries gate
    // COUNTS (gates.registered / gates.cleared), not gate events with times,
    // conditions or the sessions on either side. The gap is filed as a
    // proposed amendment to the sibling plan, not applied (the export is never
    // reshaped for a scene): coord dossier topic
    // dossier:build-record-export-scene-gaps (head 3964aa96), detailed in
    // finding 51cdb4d6.
    kinds: ["session.started", "session.ended", "gate.registered", "gate.observed", "gate.cleared"],
    entities: ["sessions", "gates"],
  },
  check: (trace) => {
    const problems: string[] = [];
    const continuations = trace.events.filter((e) => e.kind === "session.started" && e.continues !== undefined);
    if (continuations.length === 0) problems.push("at least one continuation (a session.started with `continues`)");
    const n = trace.entities.sessions.length;
    if (n > 3) problems.push(`at most three sessions (the trace has ${n})`);
    return problems;
  },
  timeline: s3Timeline,
  // The poster: the first continuation has just started, with its gate open
  // behind it and the waiting session's lane ended before it.
  poster: (timeline) => {
    const next = timeline.beats.find((b) => b.event.kind === "session.started" && b.event.continues !== undefined);
    return next ? next.startFrame + timeline.beatFrames - 1 : timeline.durationInFrames - 1;
  },
  Component: GatesContinuations,
});
