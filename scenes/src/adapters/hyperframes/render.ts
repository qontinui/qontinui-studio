/**
 * The HyperFrames implementation of the studio `EngineAdapter` (plan D7;
 * Phase 2 HyperFrames adapter spike, result in docs/hyperframes-spike.md).
 *
 * How a render runs:
 * - ./project.ts writes a self-contained HyperFrames project (index.html, the
 *   bundled page, the Inter files) into a temporary directory;
 * - the render image (docker/hyperframes/Dockerfile: HyperFrames 0.8.78 and
 *   its npm tree from a lockfile, chrome-headless-shell 149.0.7790.0 checked
 *   against a committed SHA-256, base image by digest, Debian snapshot) is
 *   resolved to its `sha256:` id after its inputs label is checked against the
 *   checked-in files (./image.ts), and renders the project with no network, a
 *   read-only root, a tmpfs /tmp, no capabilities, no-new-privileges, pid,
 *   memory and CPU limits, the studio fontconfig, and telemetry off;
 * - the result is published through ../output.ts (nothing is deleted that
 *   this adapter did not write, and nothing recursively).
 *
 * Each container gets a unique name, so a timed-out render is stopped with
 * `docker kill <name>`; killing only the docker client would leave it running.
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
import { randomBytes } from "node:crypto";
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
import { DEFAULT_HYPERFRAMES_IMAGE, INPUTS_LABEL, imageInputsSha256, imageRefProblem } from "./image";
import { PACKAGE_ROOT, writeProject, type ProjectSpec } from "./project";

export { DEFAULT_HYPERFRAMES_IMAGE } from "./image";
const FONTCONFIG_DIR = path.join(PACKAGE_ROOT, "fontconfig");
const DEFAULT_TIMEOUT_MS = 30 * 60_000;

/** Container limits; each can be overridden by its environment variable. */
const LIMITS = {
  pids: { env: "STUDIO_HF_PIDS_LIMIT", fallback: "8192", pattern: /^[1-9][0-9]*$/ },
  memory: { env: "STUDIO_HF_MEMORY", fallback: "8g", pattern: /^[1-9][0-9]*[bkmg]?$/ },
  cpus: { env: "STUDIO_HF_CPUS", fallback: "8", pattern: /^(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$/ },
} as const;

function limit(name: keyof typeof LIMITS): string {
  const { env, fallback, pattern } = LIMITS[name];
  const value = process.env[env] ?? fallback;
  if (!pattern.test(value)) throw new HyperFramesRenderError(`${env}="${value}" is not a valid ${name} limit`);
  return value;
}

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
   * else DEFAULT_HYPERFRAMES_IMAGE. Validated as an image reference.
   */
  image?: string;
  /** Capture workers inside the container (HyperFrames picks when unset). */
  workers?: number;
  /** Docker CLI to run (default "docker"). */
  docker?: string;
  /** Receives the container's output (default: kept, shown only on failure). */
  log?: (chunk: string) => void;
  /** Kill a render (its container first) after this long. Default 30 min. */
  timeoutMs?: number;
}

export class HyperFramesRenderError extends Error {
  override readonly name = "HyperFramesRenderError";
}

interface RunResult {
  code: number | null;
  output: string;
  timedOut: boolean;
}

function run(
  command: string,
  args: string[],
  options: { log?: (chunk: string) => void; onTimeout?: () => Promise<void>; timeoutMs?: number } = {},
): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    let timedOut = false;
    const take = (chunk: Buffer) => {
      const text = chunk.toString("utf8");
      output = (output + text).slice(-64_000);
      options.log?.(text);
    };
    child.stdout.on("data", take);
    child.stderr.on("data", take);
    const timer = setTimeout(() => {
      timedOut = true;
      // Stop the container first (killing the client alone leaves it running).
      void (options.onTimeout?.() ?? Promise.resolve()).finally(() => child.kill("SIGKILL"));
    }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, output, timedOut });
    });
  });
}

/**
 * A bind mount for `--mount`. Its value is comma-separated key=value pairs, so
 * a path with a comma (or a quote or newline, which change how docker parses
 * the field) is refused rather than escaped.
 */
export function bindMount(src: string, dst: string, readonly: boolean): string {
  if (/[,"\n\r]/.test(src)) {
    throw new HyperFramesRenderError(
      `refusing to mount ${JSON.stringify(src)} into the HyperFrames container: ` +
        "the path contains a comma, quote or newline, which docker --mount cannot take safely; use a path without them",
    );
  }
  return `type=bind,src=${src},dst=${dst}${readonly ? ",readonly" : ""}`;
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
  private readonly timeoutMs: number;
  /** Containers started by this adapter and not yet finished. */
  private readonly running = new Set<string>();

  constructor(options: HyperFramesAdapterOptions = {}) {
    const image = options.image ?? process.env.STUDIO_HYPERFRAMES_IMAGE ?? DEFAULT_HYPERFRAMES_IMAGE;
    const problem = imageRefProblem(image);
    if (problem) throw new HyperFramesRenderError(`HyperFrames render image: ${problem}`);
    this.image = image;
    if (options.workers !== undefined && !(Number.isInteger(options.workers) && options.workers > 0)) {
      throw new RangeError(`workers must be a positive integer (got ${options.workers})`);
    }
    this.workers = options.workers;
    this.docker = options.docker ?? "docker";
    this.log = options.log;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  /**
   * The image's `sha256:` id, after checking its inputs label against the
   * checked-in docker/hyperframes files. Refuses a missing, unlabelled or
   * stale image with the command that fixes it.
   */
  async imageId(): Promise<string> {
    const result = await run(this.docker, [
      "image",
      "inspect",
      "--format",
      "{{.Id}} {{json .Config}}",
      "--",
      this.image,
    ]);
    if (result.code !== 0) {
      throw new HyperFramesRenderError(
        `HyperFrames render image ${this.image} is not available (${result.output.trim()}). ` +
          "Build it with `pnpm hyperframes:image`.",
      );
    }
    const text = result.output.trim();
    const space = text.indexOf(" ");
    const id = space < 0 ? text : text.slice(0, space);
    let label = "";
    try {
      const config = JSON.parse(space < 0 ? "null" : text.slice(space + 1)) as { Labels?: Record<string, string> | null } | null;
      label = config?.Labels?.[INPUTS_LABEL] ?? "";
    } catch {
      label = "";
    }
    if (!/^sha256:[a-f0-9]{64}$/.test(id)) throw new HyperFramesRenderError(`unexpected image id "${id}" for ${this.image}`);
    const expected = imageInputsSha256();
    if (label !== expected) {
      throw new HyperFramesRenderError(
        `HyperFrames render image ${this.image} (${id}) was not built from the checked-in docker/hyperframes files ` +
          `(label ${INPUTS_LABEL}="${label || "<none>"}", expected ${expected}). Rebuild it with \`pnpm hyperframes:image\`.`,
      );
    }
    return id;
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

  private async killContainer(name: string): Promise<void> {
    await run(this.docker, ["kill", "--", name]).catch(() => undefined);
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
    const imageId = await this.imageId();
    // Refuse unmountable paths before writing anything.
    const fontconfigMount = bindMount(FONTCONFIG_DIR, "/studio-fontconfig", true);
    const outputMount = bindMount(outputDir, "/output", false);
    const projectDir = await mkdtemp(path.join(tmpdir(), "studio-hyperframes-"));
    const name = `studio-hf-${randomBytes(8).toString("hex")}`;
    try {
      const projectMount = bindMount(projectDir, "/project", true);
      await writeProject(projectDir, spec);
      const uid = process.getuid?.();
      const gid = process.getgid?.();
      const args = [
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--shm-size=2g",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=2g",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        limit("pids"),
        "--memory",
        limit("memory"),
        "--cpus",
        limit("cpus"),
        ...(uid !== undefined && gid !== undefined ? ["--user", `${uid}:${gid}`] : []),
        "-e",
        "HOME=/tmp",
        "-e",
        "HYPERFRAMES_NO_TELEMETRY=1",
        "-e",
        "DO_NOT_TRACK=1",
        "-e",
        "FONTCONFIG_FILE=/studio-fontconfig/fonts.conf",
        "--mount",
        fontconfigMount,
        "--mount",
        projectMount,
        "--mount",
        outputMount,
        imageId,
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
      this.running.add(name);
      const result = await run(this.docker, args, {
        log: this.log,
        onTimeout: () => this.killContainer(name),
        timeoutMs: this.timeoutMs,
      });
      if (result.timedOut) {
        throw new HyperFramesRenderError(`HyperFrames render timed out after ${this.timeoutMs} ms; container ${name} was killed`);
      }
      if (result.code !== 0) {
        throw new HyperFramesRenderError(`HyperFrames render failed (exit ${result.code}):\n${result.output.slice(-4000)}`);
      }
    } finally {
      this.running.delete(name);
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
    if (request.frames.length === 0) return [];
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

  /**
   * Nothing long-lived: each render is its own container. Closing while a
   * render is in flight (an abort) kills its container.
   */
  async close(): Promise<void> {
    await Promise.all([...this.running].map((name) => this.killContainer(name)));
  }
}
