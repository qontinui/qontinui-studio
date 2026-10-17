import { defineScene } from "../../api";
import { SelfCorrection } from "./SelfCorrection";
import { s6Timeline } from "./model";

export const s6SelfCorrection = defineScene({
  id: "s6-self-correction",
  title: "Self-correction",
  requires: {
    // Illustrative only: build-record/1 carries per-PR attempts and
    // ci_duration_secs and a self_corrections.red_ci_fixed COUNT, but no CI
    // run events with outcomes and times, and no diagnosis or fix events.
    // Filed as a proposed amendment to the sibling plan, not applied: coord
    // dossier topic dossier:build-record-export-scene-gaps, detailed in finding
    // 10f403e6.
    kinds: ["ci.started", "ci.failed", "ci.diagnosed", "fix.pushed", "ci.passed"],
    entities: ["plans", "prs"],
  },
  check: (trace) => {
    const n = trace.entities.prs.length;
    return n !== 1 ? [`exactly one PR (the trace has ${n})`] : [];
  },
  timeline: s6Timeline,
  // The poster: CI green after the loop, with the whole loop drawn and the
  // log holding the diagnosis and fix that got it there.
  poster: (timeline) => {
    const green = timeline.beats.find((b) => b.event.kind === "ci.passed");
    return green ? green.startFrame + timeline.beatFrames - 1 : timeline.durationInFrames - 1;
  },
  Component: SelfCorrection,
});
