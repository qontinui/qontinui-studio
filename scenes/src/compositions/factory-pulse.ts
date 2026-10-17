import { defineComposition } from "../api/composition";

/**
 * The weekly factory pulse (plan Phase 5): a short clip of the week, between
 * a card naming the week and a closing card that states what the clip is.
 * The pulse task fills `{{week}}` and `{{status_line}}`; it never claims a
 * replay the traces do not carry (every scene frame shows its own D2 label).
 */
export const factoryPulse = defineComposition({
  id: "factory-pulse",
  title: "Qontinui factory pulse",
  description: "The weekly pulse: S1, S6 and S4 at 1.5x between a week card and a status card.",
  segments: [
    { kind: "card", id: "open", title: "Factory pulse", body: "{{week}}", seconds: 2.5 },
    { kind: "scene", id: "lifecycle", scene: "s1-plan-lifecycle", rate: 1.5 },
    { kind: "scene", id: "self-correction", scene: "s6-self-correction", rate: 1.5 },
    { kind: "scene", id: "merge-train", scene: "s4-merge-train", rate: 1.5 },
    { kind: "card", id: "close", title: "Qontinui", body: "{{status_line}}", seconds: 3 },
  ],
});
