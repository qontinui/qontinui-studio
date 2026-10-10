import { defineScene } from "../../api";
import { PlanLifecycle } from "./PlanLifecycle";
import { s1Timeline } from "./model";

export const s1PlanLifecycle = defineScene({
  id: "s1-plan-lifecycle",
  title: "Plan lifecycle",
  requires: {
    // The minimum for a complete lifecycle; ci.failed and session.* are
    // optional (a clean run has no failure; a trace may carry no sessions).
    kinds: ["plan.drafted", "plan.vetted", "plan.implementing", "pr.opened", "ci.passed", "pr.landed", "plan.shipped"],
    entities: ["plans", "prs"],
  },
  timeline: s1Timeline,
  Component: PlanLifecycle,
});
