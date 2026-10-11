/**
 * Child process for tests/golden/interrupt.test.ts: one Remotion render,
 * close() after a delay, then a pause for any stray rejection to surface,
 * then the outcome as one JSON line on stdout.
 *
 * It runs in its own process, as a library caller would, with no
 * 'unhandledRejection' listener of its own: if the adapter let one of
 * Remotion's abandoned promises reject unhandled, Node's default crashes
 * this process (non-zero exit, no JSON), and the test fails on that.
 *
 *   node --import tsx tests/golden/interrupt-child.ts '<json args>'
 *   args: { kind, delayMs, serveUrl? }
 */
import { RemotionAdapter } from "../../src/adapters/remotion/render";
import { interruptAfter, type InterruptKind } from "./interrupt";

const STRAY_WAIT_MS = 3000;

// Diagnostics only (a monitor changes nothing): say whether a crash came
// from an unhandled rejection or a synchronous throw, and when.
const started = Date.now();
process.on("uncaughtExceptionMonitor", (err, origin) => {
  process.stderr.write(`interrupt-child: crash (${origin}, ${process.listenerCount("unhandledRejection")} rejection listener(s)) ${Date.now() - started} ms after start: ${String(err)}\n`);
});

const args = JSON.parse(process.argv[2] ?? "{}") as { kind: InterruptKind; delayMs: number; serveUrl?: string };
const adapter = new RemotionAdapter(args.serveUrl ? { serveUrl: args.serveUrl } : {});
const outcome = await interruptAfter(adapter, args.kind, args.delayMs);
await new Promise((resolve) => setTimeout(resolve, STRAY_WAIT_MS));
const error = outcome.error instanceof Error ? { name: outcome.error.name, message: outcome.error.message } : { name: "?", message: String(outcome.error) };
process.stdout.write(`${JSON.stringify({ ...outcome, error })}\n`);
// Diagnostics only: if something keeps the process alive after the outcome,
// say what (the test then times out on this child).
setTimeout(() => {
  process.stderr.write(`interrupt-child: still alive 5 s after the outcome: ${process.getActiveResourcesInfo().join(", ")}\n`);
}, 5000).unref();
