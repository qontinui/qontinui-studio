import { defineComposition } from "../api/composition";
import { INTENT_LINE, TWIN_LINE } from "./copy";

/**
 * The ~90 s explainer (plan "Marketing materials produced": S2 -> S1 -> S4 ->
 * S6): the digital twin, a plan's lifecycle, the merge train and
 * self-correction, at their own pace. Narration stretches a segment to fit
 * what is said over it, which is how it reaches its full length. Two b-roll
 * slots; either is left out when no image is bound.
 */
export const explainer = defineComposition({
  id: "explainer",
  title: "How the Qontinui factory works",
  description: "The ~90 s explainer: S2, S1, S4 and S6 between two cards, with two optional b-roll stills.",
  segments: [
    { kind: "card", id: "open", title: "How the Qontinui factory works", body: INTENT_LINE, seconds: 3 },
    { kind: "scene", id: "twin", scene: "s2-digital-twin" },
    { kind: "broll", id: "broll-twin", slot: "explainer-twin", seconds: 3 },
    { kind: "scene", id: "lifecycle", scene: "s1-plan-lifecycle" },
    { kind: "scene", id: "merge-train", scene: "s4-merge-train" },
    { kind: "broll", id: "broll-fleet", slot: "explainer-fleet", seconds: 3 },
    { kind: "scene", id: "self-correction", scene: "s6-self-correction" },
    { kind: "card", id: "close", title: "Qontinui", body: TWIN_LINE, seconds: 4 },
  ],
});
