import { defineComposition } from "../api/composition";
import { INTENT_LINE } from "./copy";

/**
 * The ~30 s teaser: the plan lifecycle, the merge train and self-correction,
 * each at twice its pace, between an opening and a closing card. One b-roll
 * slot sits before the closing card; with no image bound it is left out.
 */
export const teaser = defineComposition({
  id: "teaser",
  title: "Qontinui teaser",
  description: "A ~30 s teaser: S1, S4 and S6 at double pace between two cards, with an optional b-roll still.",
  segments: [
    { kind: "card", id: "open", title: "Qontinui", body: INTENT_LINE, seconds: 2.5 },
    { kind: "scene", id: "lifecycle", scene: "s1-plan-lifecycle", rate: 2 },
    { kind: "scene", id: "merge-train", scene: "s4-merge-train", rate: 2 },
    { kind: "scene", id: "self-correction", scene: "s6-self-correction", rate: 2 },
    { kind: "broll", id: "broll", slot: "teaser-closing", seconds: 2.5 },
    { kind: "card", id: "close", title: "Qontinui", body: "The software factory.", seconds: 3 },
  ],
});
