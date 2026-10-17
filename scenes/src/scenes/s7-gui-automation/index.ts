import { defineScene } from "../../api";
import { GuiAutomation } from "./GuiAutomation";
import { graphProblems } from "./graph-layout";
import { s7Timeline } from "./model";

export const s7GuiAutomation = defineScene({
  id: "s7-gui-automation",
  title: "Model-based GUI automation",
  requires: {
    // Illustrative by design (plan catalogue): this is the original Qontinui
    // idea, shown on a synthetic app, not a build-record replay.
    kinds: ["gui.state_found", "gui.transition_found", "gui.transition_taken"],
    entities: ["states", "transitions"],
  },
  // Only graphs the layouts can draw honestly (graph-layout.ts).
  check: graphProblems,
  timeline: s7Timeline,
  // The poster: the traversal complete, the path lit through the learned graph.
  poster: (timeline) => {
    const taken = timeline.beats.filter((b) => b.event.kind === "gui.transition_taken");
    const last = taken[taken.length - 1];
    return last ? last.startFrame + timeline.beatFrames - 1 : timeline.durationInFrames - 1;
  },
  Component: GuiAutomation,
});
