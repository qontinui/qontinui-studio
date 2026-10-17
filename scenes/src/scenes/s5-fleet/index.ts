import { defineScene } from "../../api";
import { Fleet } from "./Fleet";
import { MAX_MACHINES, SESSIONS_PER_MACHINE, s5Timeline } from "./model";

export const s5Fleet = defineScene({
  id: "s5-fleet",
  title: "The fleet",
  requires: {
    // Illustrative only: build-record/1 carries a session COUNT and excludes
    // machine names (sibling D3), with no worktree, intent or claim events.
    // Filed as a proposed amendment to the sibling plan, not applied: coord
    // dossier topic dossier:build-record-export-scene-gaps, detailed in finding
    // 45271228.
    kinds: ["session.started", "worktree.allocated", "intent.declared", "claim.acquired", "claim.contended"],
    entities: ["sessions", "machines"],
  },
  check: (trace) => {
    const problems: string[] = [];
    const { machines, sessions } = trace.entities;
    if (machines.length > MAX_MACHINES) problems.push(`at most ${MAX_MACHINES} machines (the trace has ${machines.length})`);
    const homeless = sessions.filter((s) => s.machine === undefined).map((s) => s.id);
    if (homeless.length) problems.push(`every session on a machine (not: ${homeless.join(", ")})`);
    const started = new Set(trace.events.flatMap((e) => (e.kind === "session.started" ? [e.session] : [])));
    const acting = new Set(trace.events.flatMap((e) => ("session" in e && e.session !== undefined ? [e.session] : [])));
    const unstarted = [...acting].filter((id) => !started.has(id));
    if (unstarted.length) problems.push(`a session.started for every session that acts (not: ${unstarted.join(", ")})`);
    for (const m of machines) {
      const n = sessions.filter((s) => s.machine === m.id).length;
      if (n > SESSIONS_PER_MACHINE) problems.push(`at most ${SESSIONS_PER_MACHINE} sessions per machine (${m.id} has ${n})`);
    }
    return problems;
  },
  timeline: s5Timeline,
  // The poster: the last claim taken, so every session is at work and the
  // avoided collision is counted (and, where the log has room, still listed).
  poster: (timeline) => {
    const claims = timeline.beats.filter((b) => b.event.kind === "claim.acquired");
    const last = claims[claims.length - 1];
    return last ? last.startFrame + timeline.beatFrames - 1 : timeline.durationInFrames - 1;
  },
  Component: Fleet,
});
