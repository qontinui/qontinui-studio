import { defineScene } from "../../api";
import { MergeTrain } from "./MergeTrain";
import { TRAIN_SLOTS, s4Timeline } from "./model";

export const s4MergeTrain = defineScene({
  id: "s4-merge-train",
  title: "The merge train",
  requires: {
    // Illustrative only: build-record/1 carries each PR's opened_at, landed_at,
    // ci_duration_secs and attempts, but no queue order, rebase or conflict
    // events (the catalogue's "illustrative until the export carries
    // queue/rebase order"). Filed as a proposed amendment to the sibling plan,
    // not applied: coord dossier topic dossier:build-record-export-scene-gaps,
    // detailed in finding d7207757.
    kinds: ["merge_train.queued", "merge_train.rebased", "pr.landed"],
    entities: ["plans", "prs"],
  },
  check: (trace) => {
    const n = trace.entities.prs.length;
    return n > TRAIN_SLOTS ? [`at most ${TRAIN_SLOTS} PRs (the trace has ${n})`] : [];
  },
  timeline: s4Timeline,
  // The poster: the conflicted PR has stepped out of line while the next PR
  // is tested at the head of the train.
  poster: (timeline) => {
    const conflict = timeline.beats.find((b) => b.event.kind === "merge_train.conflicted");
    return conflict ? conflict.startFrame + 2 * timeline.beatFrames - 1 : timeline.durationInFrames - 1;
  },
  Component: MergeTrain,
});
