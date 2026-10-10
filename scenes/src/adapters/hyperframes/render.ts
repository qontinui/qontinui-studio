/**
 * The HyperFrames implementation of the studio `EngineAdapter` (plan D7;
 * Phase 2 HyperFrames adapter spike, result in docs/hyperframes-spike.md).
 *
 * How a render runs:
 * - ./project.ts writes a self-contained HyperFrames project (index.html, the
 *   bundled page, the Inter files) into a temporary directory;
 * - the pinned render image (docker/hyperframes/Dockerfile: HyperFrames
 *   0.8.78, chrome-headless-shell 149.0.7790.0, base image by digest, Debian
 *   snapshot, npm lockfile) renders it with `--network none`, the studio
 *   fontconfig as FONTCONFIG_FILE, and telemetry off;
 * - the result is published through ../output.ts (nothing is deleted that
 *   this adapter did not write, and nothing recursively).
 *
 * Scenes are the same components the Remotion adapter renders, through the
 * same `SceneRoot` (./frame-adapter.tsx). No scene or primitive knows which
 * engine drew it.
 *
 * Determinism: same trace → byte-identical PNG frames across runs, worker
 * counts, render order and frames rendered alone (measured; see the spike
 * doc). Only scale 1 is supported.
 *
 * Telemetry: the HyperFrames CLI sends PostHog events unless told not to.
 * `HYPERFRAMES_NO_TELEMETRY=1` and `DO_NOT_TRACK=1` are set in the image and
 * again on every `docker run`, and the container has no network at all.
 */
import { spawn } from "node:child_process";
import { mkdtemp, readdir, rename, rmdir, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import type { EngineAdapter, OutputFormat, RenderRequest, RenderResult, StillsRequest } from "../../api/engine";
import { ASPECT_SIZES, STUDIO_FPS } from "../../api/layout";
import { missingRequirements } from "../../api/scene";
import { sceneDuration } from "../../api/scene-root";
import { fontFiles } from "../../api/typography";
import { assertRenderable } from "../../api/trace/provenance";
import type { SceneTrace } from "../../api/trace/schema";
import { getScene } from "../../scenes";
import { checkFileTarget, checkSequenceTarget, publishFile, publishSequence, withStaging } from "../output";
import { PACKAGE_ROOT, writeProject, type ProjectSpec } from "./project";

/** The image `pnpm hyperframes:image` builds from docker/hyperframes/. */
export const DEFAULT_HYPERFRAMES_IMAGE = "studio-hyperframes:0.8.78";
const FONTCONFIG_DIR = path.join(PACKAGE_ROOT, "fontconfig");
const RENDER_TIMEOUT_MS = 30 * 60_000;

/** HyperFrames' `--format` for each studio format. */
const HF_FORMATS: Record<OutputFormat, string> = {
  mp4: "mp4",
  webm: "webm",
  gif: "gif",
  png: "png-sequence",
};

export interface HyperFramesAdapterOptions {
  /**
   * Render image (tag or `sha256:` id). Default: STUDIO_HYPERFRAMES_IMAGE,
   * else DEFAULT_HYPERFRAMES_IMAGE.
   */
  image?: string;
  /** Capture workers inside the container (HyperFrames picks when unset). */
  workers?: number;
  /** Docker CLI to run (default "docker"). */
  docker?: string;
  /** Receives the container's output (default: kept, shown only on failure). */
  log?: (chunk: string) => void;
}

export class HyperFramesRenderError extends Error {
  override readonly name = "HyperFramesRenderError";
}

interface RunResult {
  code: number | null;
  output: string;
}

function run(command: string, args: string[], log?: (chunk: string) => void): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    const take = (chunk: Buffer) => {
      const text = chunk.toString("utf8");
      output = (output + text).slice(-64_000);
      log?.(text);
    };
    child.stdout.on("data", take);
    child.stderr.on("data", take);
    const timer = setTimeout(() => child.kill("SIGKILL"), RENDER_TIMEOUT_MS);
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, output });
    });
  });
}

/** Remove exactly the files ./project.ts wrote, then the (empty) directories. */
async function removeProject(dir: string): Promise<void> {
  for (const { file } of fontFiles) await unlink(path.join(dir, file)).catch(() => undefined);
  await rmdir(path.join(dir, "fonts")).catch(() => undefined);
  for (const name of ["index.html", "studio-scene.js"]) await unlink(path.join(dir, name)).catch(() => undefined);
  await rmdir(dir).catch(() => process.stderr.write(`note: left project directory ${dir} (not empty)\n`));
}

/** Move HyperFrames' `frame_000001.png …` into `element-000.png …` (0-based). */
async function adoptSequence(framesDir: string, intoDir: string, count: number): Promise<string[]> {
  const names = (await readdir(framesDir)).filter((name) => /^frame_\d+\.png$/.test(name)).sort();
  if (names.length !== count) {
    throw new HyperFramesRenderError(`HyperFrames wrote ${names.length} frames, expected ${count}`);
  }
  const width = Math.max(3, String(count - 1).length);
  const files: string[] = [];
  for (const [index, name] of names.entries()) {
    const to = path.join(intoDir, `element-${String(index).padStart(width, "0")}.png`);
    await rename(path.join(framesDir, name), to);
    files.push(to);
  }
  await rmdir(framesDir);
  return files;
}

function assertFrame(frame: number, durationInFrames: number) {
  if (!Number.isInteger(frame) || frame < 0 || frame >= durationInFrames) {
    throw new RangeError(`frame ${frame} is outside 0..${durationInFrames - 1}`);
  }
}

export class HyperFramesAdapter implements EngineAdapter {
  readonly name = "hyperframes";
  private readonly image: string;
  private readonly workers: number | undefined;
  private readonly docker: string;
  private readonly log: ((chunk: string) => void) | undefined;

  constructor(options: HyperFramesAdapterOptions = {}) {
    this.image = options.image ?? process.env.STUDIO_HYPERFRAMES_IMAGE ?? DEFAULT_HYPERFRAMES_IMAGE;
    this.workers = options.workers;
    this.docker = options.docker ?? "docker";
    this.log = options.log;
  }

  /** The image id actually used, or a clear error when it is not built. */
  async imageId(): Promise<string> {
    const result = await run(this.docker, ["image", "inspect", "--format", "{{.Id}}", this.image]);
    if (result.code !== 0) {
      throw new HyperFramesRenderError(
        `HyperFrames render image ${this.image} is not available (${result.output.trim()}). ` +
          "Build it with `pnpm hyperframes:image`.",
      );
    }
    return result.output.trim();
  }

  private check(sceneId: string, trace: SceneTrace, scale: number | undefined): number {
    assertRenderable(trace);
    if (scale !== undefined && scale !== 1) {
      throw new HyperFramesRenderError(`the HyperFrames adapter renders at scale 1 only (got ${scale})`);
    }
    const scene = getScene(sceneId);
    const problems = missingRequirements(scene, trace);
    if (problems.length) throw new Error(`trace cannot drive scene "${sceneId}"; it needs: ${problems.join("; ")}`);
    return sceneDuration(scene, trace);
  }

  /**
   * Render `frames` (scene frame per composition frame) of a project into
   * `outputDir` as `<outputName>`; HyperFrames runs at `fps`.
   */
  private async renderProject(
    spec: ProjectSpec & { fps: number },
    outputDir: string,
    outputName: string,
    format: string,
  ): Promise<void> {
    await this.imageId();
    const projectDir = await mkdtemp(path.join(tmpdir(), "studio-hyperframes-"));
    try {
      await writeProject(projectDir, spec);
      const uid = process.getuid?.();
      const gid = process.getgid?.();
      const args = [
        "run",
        "--rm",
        "--network",
        "none",
        "--shm-size=2g",
        ...(uid !== undefined && gid !== undefined ? ["--user", `${uid}:${gid}`] : []),
        "-e",
        "HOME=/tmp",
        "-e",
        "HYPERFRAMES_NO_TELEMETRY=1",
        "-e",
        "DO_NOT_TRACK=1",
        "-e",
        "FONTCONFIG_FILE=/studio-fontconfig/fonts.conf",
        "-v",
        `${FONTCONFIG_DIR}:/studio-fontconfig:ro`,
        "-v",
        `${projectDir}:/project:ro`,
        "-v",
        `${outputDir}:/output`,
        this.image,
        "/project",
        "--output",
        `/output/${outputName}`,
        "--format",
        format,
        "--fps",
        String(spec.fps),
        "--quiet",
        ...(this.workers !== undefined ? ["--workers", String(this.workers)] : []),
      ];
      const result = await run(this.docker, args, this.log);
      if (result.code !== 0) {
        throw new HyperFramesRenderError(`HyperFrames render failed (exit ${result.code}):\n${result.output.slice(-4000)}`);
      }
    } finally {
      await removeProject(projectDir);
    }
  }

  async render(request: RenderRequest): Promise<RenderResult> {
    const overwrite = request.overwrite ?? false;
    const outPath = path.resolve(request.outPath);
    const sequence = request.format === "png" && request.frame === undefined;
    if (sequence) await checkSequenceTarget(outPath, overwrite);
    else await checkFileTarget(outPath, overwrite);

    const durationInFrames = this.check(request.scene, request.trace, request.scale);
    const { width, height } = ASPECT_SIZES[request.aspect];
    const result = (files: string[]): RenderResult => ({ files, width, height, fps: STUDIO_FPS, durationInFrames });
    const all = Array.from({ length: durationInFrames }, (_, i) => i);
    const spec = { sceneId: request.scene, trace: request.trace, aspect: request.aspect };

    return withStaging(outPath, async (staging) => {
      if (request.format === "png") {
        const frames = sequence ? all : [request.frame as number];
        if (!sequence) assertFrame(request.frame as number, durationInFrames);
        await this.renderProject({ ...spec, frames, fps: STUDIO_FPS }, staging, "frames", HF_FORMATS.png);
        const files = await adoptSequence(path.join(staging, "frames"), staging, frames.length);
        request.onProgress?.(1);
        if (sequence) return result(await publishSequence(staging, outPath, overwrite));
        return result([await publishFile(files[0] as string, outPath, overwrite)]);
      }
      // GIF keeps every second frame, as the Remotion adapter does: the
      // composition then runs at 15 fps and its frame i shows scene frame 2i.
      const frames = request.format === "gif" ? all.filter((f) => f % 2 === 0) : all;
      const fps = request.format === "gif" ? STUDIO_FPS / 2 : STUDIO_FPS;
      const name = `output.${request.format}`;
      await this.renderProject({ ...spec, frames, fps }, staging, name, HF_FORMATS[request.format]);
      request.onProgress?.(1);
      return result([await publishFile(path.join(staging, name), outPath, overwrite)]);
    });
  }

  /**
   * Chosen frames in ONE container and one composition whose i-th frame is
   * the i-th requested frame (the HyperFrames CLI has no single-frame mode).
   */
  async renderStills(request: StillsRequest): Promise<string[]> {
    const overwrite = request.overwrite ?? false;
    const target = (frame: number) =>
      path.resolve(request.outDir, `${request.prefix ?? ""}f${String(frame).padStart(4, "0")}.png`);
    for (const frame of request.frames) await checkFileTarget(target(frame), overwrite);
    const durationInFrames = this.check(request.scene, request.trace, request.scale);
    for (const frame of request.frames) assertFrame(frame, durationInFrames);

    return withStaging(target(0), async (staging) => {
      await this.renderProject(
        { sceneId: request.scene, trace: request.trace, aspect: request.aspect, frames: request.frames, fps: STUDIO_FPS },
        staging,
        "frames",
        HF_FORMATS.png,
      );
      const staged = await adoptSequence(path.join(staging, "frames"), staging, request.frames.length);
      const files: string[] = [];
      for (const [index, frame] of request.frames.entries()) {
        files.push(await publishFile(staged[index] as string, target(frame), overwrite));
      }
      return files;
    });
  }

  /** Nothing long-lived: each render is its own container. */
  async close(): Promise<void> {}
}
