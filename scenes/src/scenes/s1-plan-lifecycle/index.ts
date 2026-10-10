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
  // S1 follows ONE plan and its (single) pull request.
  check: (trace) => {
    const problems: string[] = [];
    const { plans, prs } = trace.entities;
    if (plans.length !== 1) problems.push(`exactly one plan (the trace has ${plans.length})`);
    if (prs.length > 1) problems.push(`at most one PR (the trace has ${prs.length})`);
    return problems;
  },
  timeline: s1Timeline,
  Component: PlanLifecycle,
});
