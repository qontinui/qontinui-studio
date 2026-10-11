/**
 * Shared by the interrupt tests: start a render, call close() after `delayMs`,
 * and report what happened. The caller asserts that the render rejected with
 * AdapterClosed and that nothing was published after close().
 */
import { mkdtempSync, readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import type { EngineAdapter } from "../../src/api/engine";
import { SCENE_ID, loadS1Trace } from "./cases";

export type InterruptKind = "mp4" | "png-sequence" | "stills";

export interface InterruptOutcome {
  error: unknown;
  /** Entries left in the fresh output directory (render: none allowed). */
  entries: string[];
  /**
   * Entries of the output directory created, written or renamed into place
   * after close() was called (ctime: a rename updates it, mtime does not).
   */
  writtenAfterClose: string[];
  closeMs: number;
}

export async function interruptAfter(
  adapter: EngineAdapter,
  kind: InterruptKind,
  delayMs: number,
): Promise<InterruptOutcome> {
  const dir = mkdtempSync(path.join(tmpdir(), "studio-interrupt-"));
  const trace = loadS1Trace();
  const work =
    kind === "stills"
      ? adapter.renderStills({
          scene: SCENE_ID,
          trace,
          aspect: "16:9",
          frames: Array.from({ length: 60 }, (_, i) => i * 7),
          outDir: dir,
        })
      : adapter.render({
          scene: SCENE_ID,
          trace,
          aspect: "16:9",
          format: kind === "mp4" ? "mp4" : "png",
          outPath: path.join(dir, kind === "mp4" ? "out.mp4" : "out"),
        });
  const settled = work.then(
    () => null,
    (err: unknown) => err ?? new Error("rejected without a reason"),
  );
  await new Promise((resolve) => setTimeout(resolve, delayMs));
  const closedAt = Date.now();
  await adapter.close();
  const error = await settled;
  const entries = readdirSync(dir);
  const writtenAfterClose = entries.filter((name) => statSync(path.join(dir, name)).ctimeMs > closedAt);
  return { error: error ?? new Error("the render resolved: it finished before close()"), entries, writtenAfterClose, closeMs: Date.now() - closedAt };
}
