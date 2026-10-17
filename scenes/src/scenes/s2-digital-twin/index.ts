import { defineScene } from "../../api";
import { DigitalTwin } from "./DigitalTwin";
import { s2Timeline } from "./model";

export const s2DigitalTwin = defineScene({
  id: "s2-digital-twin",
  title: "The digital twin",
  requires: {
    // Illustrative only: the build-record export carries no twin, webhook or
    // question events (plan catalogue; the gap is filed against the sibling plan).
    kinds: ["twin.mirrored", "twin.decided", "question.asked", "question.answered"],
    entities: ["sources", "sessions"],
  },
  check: (trace) => {
    const n = trace.entities.sources.length;
    return n > 4 ? [`at most four sources (the trace has ${n})`] : [];
  },
  timeline: s2Timeline,
  // The poster is the moment the last question has been answered from a
  // served document: every panel is full and the operator still asleep.
  poster: (timeline) => {
    const answered = timeline.beats.filter((b) => b.event.kind === "question.answered");
    const last = answered[answered.length - 1];
    return last ? last.startFrame + timeline.beatFrames - 1 : timeline.durationInFrames - 1;
  },
  Component: DigitalTwin,
});
