/**
 * TEST-ONLY: mark a hand-built replay trace as if `fromBuildRecord` had made
 * it, so tests can render the REPLAY label (e.g. the long-source golden).
 * Refuses outside vitest. Nothing in src/ imports this module; the CLI and
 * the player cannot reach it.
 */
import { recordReplayProvenance } from "./provenance";
import type { SceneTrace } from "./schema";

export function attestReplayForTests(trace: SceneTrace): SceneTrace {
  if (process.env.VITEST !== "true") {
    throw new Error("attestReplayForTests is available only inside vitest");
  }
  return recordReplayProvenance(trace);
}
