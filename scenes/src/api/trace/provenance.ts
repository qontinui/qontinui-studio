/**
 * Replay provenance (plan D2: a synthetic trace is never presented as real).
 *
 * The schema accepts the replay SHAPE, but shape is not provenance: anyone can
 * hand-write `"mode": "replay"`. A replay may be rendered only when the trace
 * object was produced in this process by `fromBuildRecord` from a build-record
 * export. That is recorded here, by object identity, and checked by every
 * render path (the CLI, every engine adapter, the web player).
 */
import type { SceneTrace } from "./schema";

const attested = new WeakSet<SceneTrace>();

/** Called by `fromBuildRecord` (and the test-only hook) on traces it built. */
export function recordReplayProvenance(trace: SceneTrace): SceneTrace {
  attested.add(trace);
  return trace;
}

export const REPLAY_REFUSAL =
  "refusing to render a replay trace that did not come from a build-record export: " +
  "a hand-written replay would present synthetic data as real (plan D2). " +
  "Replays are rendered only via --build-record <export>, which needs the sibling plan's export (Phase 3).";

/** Why `trace` must not be rendered, or null when it may be. */
export function renderRefusal(trace: SceneTrace): string | null {
  if (trace.meta.mode === "replay" && !attested.has(trace)) return REPLAY_REFUSAL;
  return null;
}

export function assertRenderable(trace: SceneTrace): void {
  const refusal = renderRefusal(trace);
  if (refusal) throw new Error(refusal);
}
