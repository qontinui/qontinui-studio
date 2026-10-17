import { defineScene, sortEvents } from "../../api";
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
    const problems: string[] = [];
    const n = trace.entities.prs.length;
    if (n !== 1) problems.push(`exactly one PR (the trace has ${n})`);
    // The loop must close: a red run, a fix after it, and green after the last fix.
    const ordered = sortEvents(trace.events);
    const firstRed = ordered.findIndex((e) => e.kind === "ci.failed");
    const lastFix = ordered.map((e) => e.kind).lastIndexOf("fix.pushed");
    const lastPass = ordered.map((e) => e.kind).lastIndexOf("ci.passed");
    if (firstRed < 0 || lastFix < firstRed || lastPass < lastFix) {
      problems.push("a closed loop: a red run, a fix after it, and a green run after the last fix");
    }
    // "No human step" is the scene's claim; a trace with an operator answer contradicts it.
    if (trace.events.some((e) => e.kind === "question.answered" && e.from === "operator")) {
      problems.push("no operator step (the scene claims none)");
    }
    return problems;
  },
  timeline: s6Timeline,
  // The poster: the first green after a red, with the whole loop drawn and
  // the log holding the diagnosis and fix that got it there.
  poster: (timeline) => {
    const red = timeline.beats.find((b) => b.event.kind === "ci.failed");
    const green = red ? timeline.beats.find((b) => b.event.kind === "ci.passed" && b.index > red.index) : undefined;
    return green ? green.startFrame + timeline.beatFrames - 1 : timeline.durationInFrames - 1;
  },
  Component: SelfCorrection,
});
