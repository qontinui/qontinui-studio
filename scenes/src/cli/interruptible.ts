/**
 * How the CLI runs a render so that a signal stops it cleanly (shared by
 * `render` and `compose`).
 *
 * Ctrl-C / SIGTERM / SIGHUP: adapter.close() stops the render (an adapter
 * publishes nothing after close), the render unwinds through its own cleanup
 * (staging and project directories, containers, exactly as on any other
 * failure), and the CLI exits 130 / 143 / 129 saying which happened: stopped
 * with nothing published, or finished before the signal took effect. A
 * second signal exits at once, skipping cleanup — but only one that comes
 * REPEAT_SIGNAL_MS or more after the first: one Ctrl-C can arrive twice (a
 * wrapper such as pnpm or the tsx CLI relays the signal the terminal already
 * sent to the whole group), and that must not skip the cleanup.
 */
import type { EngineAdapter } from "../api/engine";

export class UsageError extends Error {}

/** The render was stopped by a signal; the process exits 128 + signal number. */
export class Interrupted extends Error {
  constructor(
    readonly signal: NodeJS.Signals,
    outcome: string,
  ) {
    super(`interrupted by ${signal}: ${outcome}`);
  }
}

/** A signal this soon after the first is the same interrupt relayed, not a second one. */
const REPEAT_SIGNAL_MS = 1000;

/**
 * The signals that stop a render: Ctrl-C, a kill, and a terminal hang-up.
 * SIGHUP matters because every docker client runs in its own process group
 * (the HyperFrames adapter), so a hang-up no longer reaches the container
 * through the client: the CLI must stop it itself.
 */
const STOP_SIGNALS = ["SIGINT", "SIGTERM", "SIGHUP"] as const;

export function exitCodeFor(signal: NodeJS.Signals): number {
  if (signal === "SIGTERM") return 143;
  if (signal === "SIGHUP") return 129;
  return 130;
}

/**
 * Run `work` (which renders through `adapter` into `outPath`), turning a
 * signal into `Interrupted` and an `OutputRefused` into a usage error, and
 * always closing the adapter.
 */
export async function runInterruptible<T>(
  adapter: EngineAdapter,
  outPath: string,
  command: string,
  work: () => Promise<T>,
): Promise<T> {
  let interrupted: NodeJS.Signals | null = null;
  let interruptedAt = 0;
  const onSignal = (signal: NodeJS.Signals) => {
    if (interrupted) {
      if (Date.now() - interruptedAt >= REPEAT_SIGNAL_MS) process.exit(exitCodeFor(signal));
      return;
    }
    interrupted = signal;
    interruptedAt = Date.now();
    process.stderr.write(`\n${signal}: stopping the render (send it again to exit at once, skipping cleanup)\n`);
    void adapter.close().catch(() => undefined);
  };
  // While stopping, an engine can reject a promise nobody awaits any more
  // (Remotion's helper processes are torn down under work it abandoned on
  // cancel, e.g. a write that fails with EPIPE). That must not crash the
  // process past the cleanup still running, so after a signal such errors
  // are reported and the shutdown continues. Before a signal they still crash.
  const onStrayRejection = (reason: unknown) => {
    if (!interrupted) throw reason;
    process.stderr.write(`note: ignored an engine error while stopping: ${reason instanceof Error ? reason.message : String(reason)}\n`);
  };
  for (const signal of STOP_SIGNALS) process.on(signal, onSignal);
  // After a hang-up the terminal is gone and writing to it fails (EIO); that
  // must not crash the process in the middle of its cleanup.
  const ignoreStreamError = () => undefined;
  process.stdout.on("error", ignoreStreamError);
  process.stderr.on("error", ignoreStreamError);
  process.on("unhandledRejection", onStrayRejection);
  try {
    const result = await work();
    if (interrupted) {
      // The render had already published when the signal took effect.
      throw new Interrupted(interrupted, `the render had already finished; ${outPath} is complete`);
    }
    return result;
  } catch (err) {
    if (err instanceof Interrupted) throw err;
    if (interrupted) {
      // AdapterClosed: close() stopped the render before it published. Any
      // other error: the render failed on its own (possibly while stopping);
      // either way publishing is the last step, so nothing reached the output.
      const what =
        err instanceof Error && err.name === "AdapterClosed"
          ? "the render was stopped"
          : `the render failed while stopping (${err instanceof Error ? err.message : String(err)})`;
      throw new Interrupted(interrupted, `${what}; nothing was published to ${outPath} and its temporary files were removed`);
    }
    if (err instanceof Error && err.name === "OutputRefused") throw new UsageError(`${command}: ${err.message}`);
    throw err;
  } finally {
    await adapter.close();
    for (const signal of STOP_SIGNALS) process.off(signal, onSignal);
    // After a signal these stay until the process exits (130 / 143 / 129).
    if (!interrupted) {
      process.stdout.off("error", ignoreStreamError);
      process.stderr.off("error", ignoreStreamError);
      process.off("unhandledRejection", onStrayRejection);
    }
  }
}
