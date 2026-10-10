/**
 * Replay adapter: the factory's build-record export → a studio scene trace.
 *
 * This is the ONLY place that knows the export's shape. The export belongs to
 * the sibling plan `2026-10-09-factory-built-product-portfolio-and-launch-kit`
 * (its Phase 1) and is never reshaped for scenes (plan D6): when a scene needs
 * a field the export lacks, the scene ships illustrative-only and the gap is
 * filed against the sibling plan.
 *
 * The real mapping lands in Phase 3, once that export exists. Until then the
 * interface is fixed here so scenes, the CLI and tests can be written against
 * it, and calling it fails loudly rather than fabricating a replay.
 *
 * The real implementation MUST return its trace through
 * `recordReplayProvenance` (./provenance): that is the only way a replay
 * becomes renderable (plan D2).
 */
import type { SceneTrace } from "./schema";

/**
 * The export version this adapter is pinned to. `null` until the sibling
 * export ships; `fromBuildRecord` refuses any record whose version differs.
 */
export const BUILD_RECORD_EXPORT_VERSION: string | null = null;

/**
 * The build-record export, as far as this adapter may assume before the
 * sibling plan publishes its schema: a versioned JSON document.
 */
export interface BuildRecordExport {
  export_version: string;
  [field: string]: unknown;
}

export interface FromBuildRecordOptions {
  /** Shown on screen as the replay's source (plan D2), e.g. a repo set. */
  sourceRef: string;
  /** Trace title shown on screen. */
  title: string;
  /** Restrict the replay to this window; defaults to the export's own. */
  window?: { from: string; to: string };
}

export function fromBuildRecord(
  record: BuildRecordExport,
  options: FromBuildRecordOptions,
): SceneTrace {
  void record;
  void options;
  throw new Error("not implemented: needs the sibling plan's build-record export");
}
