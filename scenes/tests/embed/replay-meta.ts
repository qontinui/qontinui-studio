/**
 * The REPLAY metadata the embed page gives a scene's illustrative events for
 * the replay-label check. Shared by the page and the matrix test, which
 * computes the label it expects on screen from the same function.
 */
import { SOURCE_REF_MAX } from "../../src/api/trace/schema";

/** Plainly a fixture: the label it produces must never pass for a real replay. */
export const EMBED_REPLAY_SOURCE = "TEST FIXTURE: not a real build-record export";

/** Replay meta whose date range covers every event (whole UTC days). */
export function embedReplayMeta(title: string, events: readonly { at: string }[]) {
  const times = events.map((e) => Date.parse(e.at));
  const day = 86_400_000;
  const from = Math.floor(Math.min(...times) / day) * day;
  const to = Math.floor(Math.max(...times) / day) * day + day - 1000;
  if (EMBED_REPLAY_SOURCE.length > SOURCE_REF_MAX) throw new Error("embed replay source is too long");
  return {
    mode: "replay" as const,
    title,
    source: { kind: "build-record" as const, ref: EMBED_REPLAY_SOURCE, export_version: "1" },
    date_range: { from: new Date(from).toISOString(), to: new Date(to).toISOString() },
  };
}
