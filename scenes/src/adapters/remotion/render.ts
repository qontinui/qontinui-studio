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
import path from "node:path";
import { fileURLToPath } from "node:url";

import { bundle } from "@remotion/bundler";
import {
  ensureBrowser,
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

import type {
  EngineAdapter,
  OutputFormat,
  RenderRequest,
  RenderResult,
  StillsRequest,
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

export interface RemotionAdapterOptions {
  logLevel?: LogLevel;
  /** Reuse an existing bundle (a serve URL or directory) instead of bundling. */
  serveUrl?: string;
}

/**
 * Remotion's `openBrowser` takes no environment option: it spawns Chrome with
 * `process.env` as it is at that moment. So the studio fontconfig is set only
 * for the duration of the launch and the previous value is restored in
 * `finally`; Chrome keeps the environment it was started with.
 */
async function launchBrowser(logLevel: LogLevel): Promise<HeadlessBrowser> {
  await ensureBrowser({ logLevel });
  const previous = process.env.FONTCONFIG_FILE;
  process.env.FONTCONFIG_FILE = FONTCONFIG_FILE;
  try {
    return await openBrowser("chrome", { chromiumOptions: CHROMIUM_OPTIONS, logLevel });
  } finally {
    if (previous === undefined) delete process.env.FONTCONFIG_FILE;
    else process.env.FONTCONFIG_FILE = previous;
  }
}

export class RemotionAdapter implements EngineAdapter {
  readonly name = "remotion";
  private readonly logLevel: LogLevel;
  private serveUrl: Promise<string> | null;
  private browser: Promise<HeadlessBrowser> | null = null;

  constructor(options: RemotionAdapterOptions = {}) {
    this.logLevel = options.logLevel ?? "error";
    this.serveUrl = options.serveUrl ? Promise.resolve(options.serveUrl) : null;
  }

  /** Bundle the scenes once per adapter instance. */
  private getServeUrl(): Promise<string> {
    this.serveUrl ??= bundle({ entryPoint: ENTRY_POINT, publicDir: PUBLIC_DIR });
    return this.serveUrl;
  }

  private getBrowser(): Promise<HeadlessBrowser> {
    this.browser ??= launchBrowser(this.logLevel);
    return this.browser;
  }

  private async composition(sceneId: string, trace: SceneTrace, aspect: Aspect) {
    assertRenderable(trace);
    const scene = getScene(sceneId);
    const problems = missingRequirements(scene, trace);
    if (problems.length) {
      throw new Error(`trace cannot drive scene "${sceneId}"; it needs: ${problems.join("; ")}`);
    }
    const inputProps: StudioCompositionProps = { sceneId, trace, aspect };
    const serveUrl = await this.getServeUrl();
    const puppeteerInstance = await this.getBrowser();
    const composition = await selectComposition({
      serveUrl,
      id: sceneId,
      inputProps,
      puppeteerInstance,
      chromiumOptions: CHROMIUM_OPTIONS,
      logLevel: this.logLevel,
    });
    return { serveUrl, puppeteerInstance, composition, inputProps };
  }

  async render(request: RenderRequest): Promise<RenderResult> {
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
      serveUrl,
      composition,
      inputProps,
      puppeteerInstance,
      chromiumOptions: CHROMIUM_OPTIONS,
      logLevel: this.logLevel,
      scale,
    };
    const result = (files: string[]): RenderResult => ({
      files,
      width: Math.round(composition.width * scale),
      height: Math.round(composition.height * scale),
      fps: composition.fps,
      durationInFrames: composition.durationInFrames,
    });

    return withStaging(outPath, async (staging) => {
      if (sequence) {
        await renderFrames({
          ...common,
          outputDir: staging,
          imageFormat: "png",
          onStart: () => undefined,
          onFrameUpdate: (rendered) => request.onProgress?.(rendered / composition.durationInFrames),
        });
        return result(await publishSequence(staging, outPath, overwrite));
      }

      const staged = path.join(staging, `output.${request.format}`);
      if (request.format === "png") {
        const frame = request.frame as number;
        assertFrame(frame, composition.durationInFrames);
        await renderStill({ ...common, output: staged, frame, imageFormat: "png" });
      } else {
        await renderMedia({
          ...common,
          codec: CODECS[request.format],
          outputLocation: staged,
          // GIF at half rate keeps files sane; video keeps every frame.
          everyNthFrame: request.format === "gif" ? 2 : 1,
          onProgress: ({ progress }) => request.onProgress?.(progress),
        });
      }
      return result([await publishFile(staged, outPath, overwrite)]);
    });
  }

  async renderStills(request: StillsRequest): Promise<string[]> {
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
        await renderStill({
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
        });
        files.push(await publishFile(staged, target(frame), overwrite));
      }
      return files;
    });
  }

  async close(): Promise<void> {
    if (this.browser) {
      const browser = await this.browser;
      this.browser = null;
      await browser.close({ silent: true });
    }
  }
}

function assertFrame(frame: number, durationInFrames: number) {
  if (!Number.isInteger(frame) || frame < 0 || frame >= durationInFrames) {
    throw new RangeError(`frame ${frame} is outside 0..${durationInFrames - 1}`);
  }
}
