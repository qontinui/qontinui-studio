/**
 * close() during a Remotion render, at several delays (plan Phase 2 review,
 * F2): the render rejects with AdapterClosed, and nothing is published after
 * close(). A render or sequence leaves its output directory empty (no
 * output, no staging); renderStills may keep stills published before close().
 * Delays cover the bundle, the browser launch and selectComposition, the
 * render itself, and the gaps between stills.
 *
 * Each case runs in a child process (./interrupt-child.ts) with no
 * 'unhandledRejection' listener, as a library caller would: it must also
 * exit cleanly, i.e. no promise Remotion abandoned on cancel may crash it.
 * (In-process, vitest's own listener would report those even when the
 * adapter absorbs them; see absorbCancelNoise in the adapter.)
 */
import { execFile } from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";

import { describe, expect, it } from "vitest";

import { RemotionAdapter } from "../../src/adapters/remotion/render";
import { PACKAGE_ROOT } from "./cases";
import type { InterruptKind, InterruptOutcome } from "./interrupt";

const TIMEOUT_MS = 10 * 60_000;
const CHILD = path.join(PACKAGE_ROOT, "tests/golden/interrupt-child.ts");

let serveUrl: Promise<string> | null = null;
const bundled = () => (serveUrl ??= RemotionAdapter.bundle());

const CASES: { kind: InterruptKind; delayMs: number; bundle: boolean }[] = [
  { kind: "mp4", delayMs: 0, bundle: true },
  { kind: "mp4", delayMs: 300, bundle: true },
  { kind: "mp4", delayMs: 2000, bundle: true },
  { kind: "mp4", delayMs: 0, bundle: false },
  { kind: "mp4", delayMs: 800, bundle: false },
  { kind: "mp4", delayMs: 5000, bundle: false },
  { kind: "png-sequence", delayMs: 4000, bundle: false },
  { kind: "stills", delayMs: 2500, bundle: false },
  { kind: "stills", delayMs: 6000, bundle: false },
];

type ChildOutcome = Omit<InterruptOutcome, "error"> & { error: { name: string; message: string } };

/**
 * Run one case in a child process (asynchronously: a blocked worker misses
 * vitest's own RPC deadlines). A non-zero exit (a crash) rejects, failing
 * the case; the child's stderr is shown with it.
 */
async function runChild(args: { kind: InterruptKind; delayMs: number; serveUrl?: string }): Promise<ChildOutcome> {
  const { stdout } = await promisify(execFile)(process.execPath, ["--import", "tsx", CHILD, JSON.stringify(args)], {
    cwd: PACKAGE_ROOT,
    encoding: "utf8",
    timeout: TIMEOUT_MS,
    maxBuffer: 16 * 1024 * 1024,
  });
  const line = stdout.trim().split("\n").pop() ?? "";
  return JSON.parse(line) as ChildOutcome;
}

describe("Remotion adapter: close() mid-render", () => {
  it.each(CASES)("$kind, close after $delayMs ms (bundling: $bundle)", async ({ kind, delayMs, bundle }) => {
    const outcome = await runChild({ kind, delayMs, serveUrl: bundle ? undefined : await bundled() });
    expect(outcome.error.name).toBe("AdapterClosed");
    expect(outcome.writtenAfterClose).toEqual([]);
    if (kind !== "stills") expect(outcome.entries).toEqual([]);
  }, TIMEOUT_MS);
});
