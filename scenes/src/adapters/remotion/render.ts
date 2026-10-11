/**
 * The Remotion implementation of the studio `EngineAdapter` (plan D7).
 *
 * Determinism (same trace → same frames):
 * - the headless browser is the Chrome Headless Shell build pinned by the
 *   exact `@remotion/renderer` version in package.json (`ensureBrowser`);
 * - software GL (`swangle`), so the GPU of the machine does not matter;
 * - a fixed frame rate (`STUDIO_FPS`) and frame-seeked rendering;
 * - fonts bundled from `assets/fonts/`, never fetched from the web, and the
 *   browser started under the studio's own fontconfig (`fontconfig/fonts.conf`:
 *   no system fonts, fixed antialiasing/hinting) instead of the machine's.
 *   Frames were byte-identical on the Debian dev host and a local ubuntu:24.04
 *   container; CI is the authority beyond those two;
 * - scenes are pure functions of `(trace, frame)` (enforced by lint).
 *
 * Output safety: see ../output.ts — nothing is deleted that this adapter did
 * not write, and nothing recursively.
 *
 * Replays: every entry point refuses a replay trace without build-record
 * provenance (../../api/trace/provenance.ts, plan D2).
 *
 * Licensing/telemetry: no `licenseKey` is passed, so Remotion sends no usage
 * event; the free-licence eligibility is recorded in plan Phase 0. No cloud
 * or Lambda rendering is used.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { bundle } from "@remotion/bundler";
import {
  ensureBrowser,
  makeCancelSignal,
  openBrowser,
  renderFrames,
  renderMedia,
  renderStill,
  selectComposition,
  type ChromiumOptions,
  type Codec,
  type HeadlessBrowser,
  type LogLevel,
} from "@remotion/renderer";

import {
  AdapterClosed,
  type EngineAdapter,
  type OutputFormat,
  type RenderRequest,
  type RenderResult,
  type StillsRequest,
} from "../../api/engine";
import type { Aspect } from "../../api/layout";
import { missingRequirements } from "../../api/scene";
import { assertRenderable } from "../../api/trace/provenance";
import type { SceneTrace } from "../../api/trace/schema";
import { getScene } from "../../scenes";
import {
  checkFileTarget,
  checkSequenceTarget,
  publishFile,
  publishSequence,
  withStaging,
} from "../output";
import type { StudioCompositionProps } from "./StudioComposition";

const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const ENTRY_POINT = path.join(PACKAGE_ROOT, "src/adapters/remotion/entry.ts");
const PUBLIC_DIR = path.join(PACKAGE_ROOT, "assets");
const FONTCONFIG_FILE = path.join(PACKAGE_ROOT, "fontconfig/fonts.conf");

const CHROMIUM_OPTIONS: ChromiumOptions = { gl: "swangle" };

const CODECS: Record<Exclude<OutputFormat, "png">, Codec> = {
  mp4: "h264",
  webm: "vp8",
  gif: "gif",
};

/**
 * Who handles SIGINT. "remotion" (default): Remotion's own listener kills
 * Chrome and exits 130 at once, so a library caller with no handler still
 * leaves no Chrome behind (but no staging cleanup runs). "caller": the caller
 * handles SIGINT and calls close() (the CLI does), so that listener is
 * removed and the render unwinds through its cleanup.
 */
export type Interrupts = "remotion" | "caller";

export interface RemotionAdapterOptions {
  /** See `Interrupts`. Default "remotion". */
  interrupts?: Interrupts;
  logLevel?: LogLevel;
  /** Reuse an existing bundle (a serve URL or directory) instead of bundling. */
  serveUrl?: string;
  /**
   * Port for Remotion's local file server. Default: a free port picked per
   * adapter, so concurrent renders (other processes, other adapters) do not
   * contend for Remotion's default port range.
   */
  port?: number;
}

/** A port the OS reports free right now. */
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => (port ? resolve(port) : reject(new Error("could not find a free port"))));
    });
  });
}

/** How long close() waits for cancelled renders to unwind before closing the browser. */
const CLOSE_UNWIND_MS = 15_000;

/** The one failure retried: Remotion's server port lost to another process. */
const PORT_CONTENTION = /but got no response/;

/**
 * Remotion's `openBrowser` takes no environment option: it spawns Chrome with
 * `process.env` as it is at that moment. So the studio fontconfig is set only
 * for the duration of the launch and the previous value is restored in
 * `finally`; Chrome keeps the environment it was started with.
 */
async function launchBrowser(logLevel: LogLevel, interrupts: Interrupts): Promise<HeadlessBrowser> {
  await ensureBrowser({ logLevel });
  const previous = process.env.FONTCONFIG_FILE;
  process.env.FONTCONFIG_FILE = FONTCONFIG_FILE;
  try {
    const browser = await openBrowser("chrome", { chromiumOptions: CHROMIUM_OPTIONS, logLevel });
    if (interrupts === "caller") removeRemotionSigintExit();
    return browser;
  } finally {
    if (previous === undefined) delete process.env.FONTCONFIG_FILE;
    else process.env.FONTCONFIG_FILE = previous;
  }
}

/**
 * Remotion's launcher (@remotion/renderer BrowserRunner.js) adds a SIGINT
 * listener that kills Chrome and calls process.exit(130) at once. For a
 * caller with no interrupt handling that is the right default: Ctrl-C still
 * kills Chrome. A caller that handles SIGINT itself (the CLI: it closes the
 * adapter and lets the render unwind through its staging cleanup) asks for
 * `interrupts: "caller"`, and only then is that one listener removed after
 * each launch, identified by its source (`killProcess(); process.exit(130);`)
 * so no other listener is touched. Remotion's 'exit' listener, which kills
 * Chrome on any exit, always stays; Remotion removes both itself when the
 * browser closes.
 */
export const REMOTION_SIGINT_EXIT = /killProcess\(\);\s*process\.exit\(130\)/;
export function removeRemotionSigintExit(): void {
  for (const listener of process.listeners("SIGINT")) {
    if (REMOTION_SIGINT_EXIT.test(String(listener))) process.off("SIGINT", listener);
  }
}

export class RemotionAdapter implements EngineAdapter {
  readonly name = "remotion";
  private readonly logLevel: LogLevel;
  private readonly interrupts: Interrupts;
  private serveUrl: Promise<string> | null;
  private browser: Promise<HeadlessBrowser> | null = null;
  private port: Promise<number>;
  /** Cancels in-flight renders when close() is called (e.g. on Ctrl-C). */
  private readonly cancel = makeCancelSignal();
  /** Set by close(); every phase checks it (single-use, see EngineAdapter). */
  private closed = false;
  private closing: Promise<void> | null = null;
  /** The selectComposition in flight: close() waits for it (see close()). */
  private selecting: Promise<unknown> | null = null;
  /** Renders in flight: close() waits (bounded) for them to unwind. */
  private readonly inFlight = new Set<Promise<unknown>>();
  /** Stills in flight: close() lets them finish before closing the browser (see still()). */
  private readonly stills = new Set<Promise<unknown>>();
  /** Settles once close() has closed the browser (see rendering()). */
  private browserDown: Promise<void> | null = null;

  constructor(options: RemotionAdapterOptions = {}) {
    this.logLevel = options.logLevel ?? "error";
    this.interrupts = options.interrupts ?? "remotion";
    this.serveUrl = options.serveUrl ? Promise.resolve(options.serveUrl) : null;
    this.port = options.port !== undefined ? Promise.resolve(options.port) : freePort();
  }

  /**
   * Run one Remotion call on this adapter's port. If the page got no response
   * (another render took the port between our check and Remotion's bind),
   * pick a fresh free port and retry once. Nothing else is retried.
   */
  private async onPort<T>(call: (port: number) => Promise<T>): Promise<T> {
    this.assertOpen();
    try {
      return await call(await this.port);
    } catch (err) {
      if (this.closed) throw new AdapterClosed(this.name);
      if (!(err instanceof Error) || !PORT_CONTENTION.test(err.message)) throw err;
      this.port = freePort();
      this.assertOpen();
      return call(await this.port);
    }
  }

  private assertOpen(): void {
    if (this.closed) throw new AdapterClosed(this.name);
  }

  /**
   * A render call (renderFrames, renderStill, renderMedia) on this adapter's
   * port. When close() stopped it, Remotion rejects at once but frames it
   * had already started can still be written into the staging directory;
   * so the rejection is held until close() has closed the browser (no more
   * frames), and only then does the caller's staging cleanup run.
   */
  /**
   * One renderStill. It is NOT given the cancel signal: Remotion's cancel
   * path for renderStill closes the page but can leave the call itself
   * pending forever, so the file server it started is never closed and
   * keeps a library caller's process alive (measured: 3 in 24 runs). A still
   * is one frame, so close() instead lets it finish (bounded by
   * CLOSE_UNWIND_MS) before closing the browser, and the result is not
   * published (every caller checks assertOpen() before publishing).
   */
  private still<T>(work: Promise<T>): Promise<T> {
    this.stills.add(work);
    const done = () => this.stills.delete(work);
    void work.then(done, done);
    return work;
  }

  private async rendering<T>(call: (port: number) => Promise<T>): Promise<T> {
    try {
      return await this.onPort(call);
    } catch (err) {
      if (!this.closed) throw err;
      await this.browserDown?.catch(() => undefined);
      throw new AdapterClosed(this.name);
    }
  }

  /**
   * Bundle the scenes and return the serve URL, so several adapters (each
   * with its own fresh browser) can share one bundle.
   */
  static bundle(): Promise<string> {
    return bundle({ entryPoint: ENTRY_POINT, publicDir: PUBLIC_DIR });
  }

  /** Bundle the scenes once per adapter instance. */
  private getServeUrl(): Promise<string> {
    this.serveUrl ??= RemotionAdapter.bundle();
    return this.serveUrl;
  }

  private getBrowser(): Promise<HeadlessBrowser> {
    this.browser ??= launchBrowser(this.logLevel, this.interrupts);
    return this.browser;
  }

  private async composition(sceneId: string, trace: SceneTrace, aspect: Aspect) {
    this.assertOpen();
    assertRenderable(trace);
    const scene = getScene(sceneId);
    const problems = missingRequirements(scene, trace);
    if (problems.length) {
      throw new Error(`trace cannot drive scene "${sceneId}"; it needs: ${problems.join("; ")}`);
    }
    const inputProps: StudioCompositionProps = { sceneId, trace, aspect };
    const serveUrl = await this.getServeUrl();
    this.assertOpen();
    const puppeteerInstance = await this.getBrowser();
    this.assertOpen();
    const selecting = this.onPort((port) => selectComposition({
      port,
      serveUrl,
      id: sceneId,
      inputProps,
      puppeteerInstance,
      chromiumOptions: CHROMIUM_OPTIONS,
      logLevel: this.logLevel,
    }));
    this.selecting = selecting;
    const composition = await selecting;
    this.assertOpen();
    return { serveUrl, puppeteerInstance, composition, inputProps };
  }

  /**
   * Track a render so close() can wait for it. The bookkeeping hangs off an
   * internal branch; the caller gets a separate promise derived from `work`
   * with no rejection handler, so a caller that never awaits a failing
   * render still gets Node's unhandled-rejection behaviour for it.
   */
  private track<T>(work: Promise<T>): Promise<T> {
    const settled = work.then(
      () => undefined,
      () => undefined,
    );
    this.inFlight.add(settled);
    void settled.then(() => this.inFlight.delete(settled));
    return work.then((value) => value);
  }

  render(request: RenderRequest): Promise<RenderResult> {
    return this.track(renderContext.run(this, () => this.renderUntracked(request)));
  }

  private async renderUntracked(request: RenderRequest): Promise<RenderResult> {
    this.assertOpen();
    const overwrite = request.overwrite ?? false;
    const outPath = path.resolve(request.outPath);
    const sequence = request.format === "png" && request.frame === undefined;
    // Refuse before any work if the target is unsafe; re-checked at publish.
    if (sequence) await checkSequenceTarget(outPath, overwrite);
    else await checkFileTarget(outPath, overwrite);

    const { serveUrl, puppeteerInstance, composition, inputProps } = await this.composition(
      request.scene,
      request.trace,
      request.aspect,
    );
    const scale = request.scale ?? 1;
    const common = {
      cancelSignal: this.cancel.cancelSignal,
      serveUrl,
      composition,
      inputProps,
      puppeteerInstance,
      chromiumOptions: CHROMIUM_OPTIONS,
      logLevel: this.logLevel,
      scale,
    };
    // The caller's callback runs outside the render's async context, so a
    // rejection it causes is never attributed to this render (see
    // renderContext below).
    const progress = (fraction: number) => renderContext.exit(() => request.onProgress?.(fraction));
    const result = (files: string[]): RenderResult => ({
      files,
      width: Math.round(composition.width * scale),
      height: Math.round(composition.height * scale),
      fps: composition.fps,
      durationInFrames: composition.durationInFrames,
    });

    return withStaging(outPath, async (staging) => {
      if (sequence) {
        await this.rendering((port) => renderFrames({
          ...common,
          port,
          outputDir: staging,
          imageFormat: "png",
          onStart: () => undefined,
          onFrameUpdate: (rendered) => progress(rendered / composition.durationInFrames),
        }));
        this.assertOpen();
        return result(await publishSequence(staging, outPath, overwrite));
      }

      const staged = path.join(staging, `output.${request.format}`);
      if (request.format === "png") {
        const frame = request.frame as number;
        assertFrame(frame, composition.durationInFrames);
        const { cancelSignal: _uncancellable, ...still } = common;
        void _uncancellable;
        await this.rendering((port) => this.still(renderStill({ ...still, port, output: staged, frame, imageFormat: "png" })));
      } else {
        const codec = CODECS[request.format];
        const everyNthFrame = request.format === "gif" ? 2 : 1;
        await this.rendering((port) => renderMedia({
          ...common,
          port,
          codec,
          outputLocation: staged,
          // GIF at half rate keeps files sane; video keeps every frame.
          everyNthFrame,
          onProgress: ({ progress: fraction }) => progress(fraction),
        }));
      }
      this.assertOpen();
      return result([await publishFile(staged, outPath, overwrite)]);
    });
  }

  renderStills(request: StillsRequest): Promise<string[]> {
    return this.track(renderContext.run(this, () => this.renderStillsUntracked(request)));
  }

  private async renderStillsUntracked(request: StillsRequest): Promise<string[]> {
    this.assertOpen();
    const overwrite = request.overwrite ?? false;
    const target = (frame: number) =>
      path.resolve(request.outDir, `${request.prefix ?? ""}f${String(frame).padStart(4, "0")}.png`);
    for (const frame of request.frames) await checkFileTarget(target(frame), overwrite);

    const { serveUrl, puppeteerInstance, composition, inputProps } = await this.composition(
      request.scene,
      request.trace,
      request.aspect,
    );
    for (const frame of request.frames) assertFrame(frame, composition.durationInFrames);

    return withStaging(target(0), async (staging) => {
      const files: string[] = [];
      for (const frame of request.frames) {
        const staged = path.join(staging, `f${frame}.png`);
        await this.rendering((port) => this.still(renderStill({
          port,
          serveUrl,
          composition,
          inputProps,
          puppeteerInstance,
          chromiumOptions: CHROMIUM_OPTIONS,
          logLevel: this.logLevel,
          scale: request.scale ?? 1,
          output: staged,
          frame,
          imageFormat: "png",
        })));
        this.assertOpen();
        files.push(await publishFile(staged, target(frame), overwrite));
      }
      return files;
    });
  }

  /**
   * Single-use (see EngineAdapter). close():
   * 1. marks the adapter closed (every phase checks it; nothing is published
   *    after close) and cancels any render in flight, unconditionally;
   * 2. waits for a selectComposition in flight (closing the browser under it
   *    makes Remotion leak the file server it started, which keeps a library
   *    caller's process alive), then closes the browser, which stops frames
   *    that were already being captured;
   * 3. waits, for at most CLOSE_UNWIND_MS, for the cancelled renders to
   *    unwind through their staging cleanup, so a caller that exits right
   *    after close() leaves nothing behind.
   * A render still bundling is not interrupted (bundling has no cancel); it
   * stops, unpublished, as soon as the bundle returns. Idempotent.
   */
  async close(): Promise<void> {
    this.closed = true;
    this.closing ??= (async () => {
      if (this.inFlight.size) listenForCancelNoise(this);
      // Remotion's cancel callbacks start cleanup work it never awaits; run
      // them in this adapter's render context so what they leave rejecting
      // is attributed to it (see renderContext).
      renderContext.run(this, () => this.cancel.cancel());
      this.browserDown = (async () => {
        await this.selecting?.catch(() => undefined);
        await Promise.race([
          Promise.allSettled([...this.stills]),
          new Promise((resolve) => setTimeout(resolve, CLOSE_UNWIND_MS).unref()),
        ]);
        const browser = this.browser ? await this.browser.catch(() => null) : null;
        this.browser = null;
        // Remotion's close kills Chrome first, then queries its pages over
        // the dead connection, so it can reject ("Target closed") after Chrome
        // is gone. That must not make close() itself reject (the CLI would
        // exit 1 instead of 130); Remotion's 'exit' listener still kills a
        // Chrome that somehow survived, when the process exits.
        if (browser) await browser.close({ silent: true }).catch(() => undefined);
      })();
      await this.browserDown;
      await Promise.race([
        Promise.allSettled([...this.inFlight]),
        new Promise((resolve) => setTimeout(resolve, CLOSE_UNWIND_MS).unref()),
      ]);
    })();
    return this.closing;
  }
}

/**
 * Rejections Remotion abandons on cancel (4.0.529). Cancelling a render
 * leaves some of Remotion's own promises rejecting later with nobody
 * awaiting them, e.g. renderStill's un-awaited page cleanup ("Target
 * closed" once the browser is gone) and renderFrames' un-awaited server
 * cleanup (an EPIPE writing to a helper process that already quit). By
 * default Node crashes the process on those, after close() has done its job.
 *
 * They cannot be caught where they arise (Remotion holds the promises), so
 * they are ATTRIBUTED instead: every render call runs inside
 * `renderContext.run(adapter, …)`, and Node runs an 'unhandledRejection'
 * listener in the async context in which the promise was rejected. A
 * rejection is absorbed only when that context is a render of an adapter
 * that close() has cancelled. The caller's own callbacks (onProgress) run
 * outside that context, so their rejections are never attributed to it.
 *
 * Everything else gets what Node would have done without this listener:
 * if another 'unhandledRejection' listener exists, it handles it; otherwise
 * the --unhandled-rejections mode (from process.execArgv and NODE_OPTIONS)
 * decides: "throw" (the default) raises it as an uncaught exception, as Node
 * does; "warn" emits Node's warning; "warn-with-error-code" also sets exit
 * code 1; "none" does nothing; under "strict" Node has already raised it
 * before any listener runs.
 *
 * The listener is installed by close() of an adapter with a render in flight
 * and removed CANCEL_NOISE_WINDOW_MS after the last such close(). Where Node
 * does not restore the async context for the listener (before Node 24's
 * AsyncContextFrame), nothing is attributed and nothing is absorbed.
 */
const renderContext = new AsyncLocalStorage<object>();
const cancelledAdapters = new WeakSet<object>();
const CANCEL_NOISE_WINDOW_MS = 30_000;
let listenUntil = 0;
let listening = false;

/** The --unhandled-rejections mode Node runs with (last flag wins; default "throw"). */
export function unhandledRejectionsMode(
  execArgv: readonly string[] = process.execArgv,
  nodeOptions: string = process.env.NODE_OPTIONS ?? "",
): string {
  let mode = "throw";
  // Node accepts both `--unhandled-rejections=<mode>` and
  // `--unhandled-rejections <mode>` (two arguments), in either source.
  const args = [...nodeOptions.split(/\s+/).filter(Boolean), ...execArgv];
  for (const [index, arg] of args.entries()) {
    if (arg === "--unhandled-rejections") {
      const next = args[index + 1];
      if (next !== undefined && !next.startsWith("-")) mode = next;
      continue;
    }
    const match = /^--unhandled-rejections=(.+)$/.exec(arg);
    if (match?.[1]) mode = match[1];
  }
  return mode;
}

/** Whether the current async context is a render of a cancelled adapter. */
export function isCancelledRender(): boolean {
  const adapter = renderContext.getStore();
  return adapter !== undefined && cancelledAdapters.has(adapter);
}

function onCancelRejection(reason: unknown, promise: Promise<unknown>): void {
  if (isCancelledRender()) return;
  if (process.listenerCount("unhandledRejection") > 1) return;
  const mode = unhandledRejectionsMode();
  if (mode === "throw") throw reason;
  // "warn": Node itself always warns, listener or not; nothing to add.
  // "warn-with-error-code": Node warns and sets exit code 1 only when no
  // listener handled it, and this listener did, so it does both itself.
  if (mode === "warn-with-error-code") {
    const detail = reason instanceof Error ? (reason.stack ?? String(reason)) : String(reason);
    process.emitWarning(detail, "UnhandledPromiseRejectionWarning");
    process.emitWarning(
      "Unhandled promise rejection. This error originated either by throwing inside of an async function " +
        "without a catch block, or by rejecting a promise which was not handled with .catch().",
      "UnhandledPromiseRejectionWarning",
    );
    process.exitCode = 1;
  }
  void promise;
}

function listenForCancelNoise(adapter: object): void {
  cancelledAdapters.add(adapter);
  listenUntil = Date.now() + CANCEL_NOISE_WINDOW_MS;
  if (listening) return;
  listening = true;
  process.on("unhandledRejection", onCancelRejection);
  const release = () => {
    if (Date.now() < listenUntil) {
      setTimeout(release, listenUntil - Date.now()).unref();
      return;
    }
    process.off("unhandledRejection", onCancelRejection);
    listening = false;
  };
  setTimeout(release, CANCEL_NOISE_WINDOW_MS).unref();
}

function assertFrame(frame: number, durationInFrames: number) {
  if (!Number.isInteger(frame) || frame < 0 || frame >= durationInFrames) {
    throw new RangeError(`frame ${frame} is outside 0..${durationInFrames - 1}`);
  }
}

/** Test seam for the cancel-noise attribution (tests/unit/adapter-lifecycle.test.ts). */
export const cancelNoiseForTests = {
  runAsRender: <T>(adapter: object, fn: () => T): T => renderContext.run(adapter, fn),
  listen: (adapter: object): void => listenForCancelNoise(adapter),
};
