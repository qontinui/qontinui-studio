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
 * Container lifecycle. Each container gets a unique name and two labels (this
 * render, this adapter). It is made with `docker create` and run with
 * `docker start --attach`; every docker client runs in its own process group
 * (`detached`), so a terminal Ctrl-C reaches only node and never kills a
 * client mid-create. A timed-out render is stopped with `docker kill <name>`
 * (killing only the client would leave it running). After every render, a
 * sweep removes its container by name and label; after an unclean create it
 * keeps looking for LATE_CREATE_WINDOW_MS, because the daemon can finish a
 * create after its client is gone. close() kills what is running, waits for
 * the renders to unwind, and sweeps by the adapter label. Every cleanup call
 * is bounded. The container also has its own deadline (render timeout plus
 * 60 s, enforced by `timeout` in studio-hf-render.sh), so it ends even when
 * nothing is left to stop it (e.g. the CLI was SIGKILLed).
 *
 * The image renders into its own tmpfs and copies only the finished artifact
 * into the staging directory (docker/hyperframes/studio-hf-render.sh), so a
 * failed or interrupted render leaves at most `<name>` or `.partial-<name>`
 * there: a file, or a directory of plain files. cleanContainerOutput removes
 * exactly those, one level deep.
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
 * again on every container this adapter creates, and the container has no
 * network at all.
 */
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { lstat, mkdtemp, readdir, rename, rmdir, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import {
  AdapterClosed,
  type EngineAdapter,
  type OutputFormat,
  type RenderRequest,
  type RenderResult,
  type StillsRequest,
} from "../../api/engine";
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
/** `docker create` returns in seconds; this only bounds a hung daemon. */
const CREATE_TIMEOUT_MS = 5 * 60_000;
/** Every docker call on a cleanup path (kill, rm, ps, inspect) is bounded by this. */
const CLEANUP_CALL_TIMEOUT_MS = 15_000;
/** How long a sweep waits for a container a killed `docker create` may still produce. */
const LATE_CREATE_WINDOW_MS = 10_000;
/** Upper bound on one sweep, whatever happens: each call in it is cut at this deadline too. */
const SWEEP_MAX_MS = 60_000;
/** Labels on every container: this render, and the adapter that made it. */
const RUN_LABEL = "org.qontinui.studio.run";
export const ADAPTER_LABEL = "org.qontinui.studio.adapter";

/**
 * Container limits; each can be overridden by its environment variable.
 *
 * The render tmpfs (`/tmp`, STUDIO_HF_TMPFS) is memory: what HyperFrames
 * writes there (its work directory, captured frames, the finished artifact
 * before it is copied out) counts against `--memory` (STUDIO_HF_MEMORY),
 * together with Chrome's own use. A 1080p S1 render needs well under 1 GB of
 * tmpfs. For long or 4K renders raise STUDIO_HF_TMPFS (a 4K PNG frame is ~4x
 * a 1080p one) and raise STUDIO_HF_MEMORY by at least as much, or the
 * container is OOM-killed instead of the tmpfs filling up.
 */
const LIMITS = {
  pids: { env: "STUDIO_HF_PIDS_LIMIT", fallback: "8192", pattern: /^[1-9][0-9]*$/ },
  memory: { env: "STUDIO_HF_MEMORY", fallback: "8g", pattern: /^[1-9][0-9]*[bkmg]?$/ },
  tmpfs: { env: "STUDIO_HF_TMPFS", fallback: "4g", pattern: /^[1-9][0-9]*[kmg]?$/ },
  cpus: { env: "STUDIO_HF_CPUS", fallback: "8", pattern: /^(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$/ },
} as const;

/** Margin of the container's own deadline over the adapter's render timeout. */
const CONTAINER_DEADLINE_MARGIN_S = 60;

/** The container's own deadline: the adapter's render timeout plus a margin. */
export function containerDeadlineSeconds(timeoutMs: number): number {
  return Math.ceil(timeoutMs / 1000) + CONTAINER_DEADLINE_MARGIN_S;
}

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
  /** Kill a `docker create` that has not returned after this long. Default 5 min. */
  createTimeoutMs?: number;
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
    // detached: the docker client gets its own process group, so a terminal
    // Ctrl-C (sent to the whole foreground group) reaches only node, whose
    // close() then drives the teardown. Killing a client mid-`create` is what
    // leaves a container in `created` behind.
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"], detached: true });
    const killClient = () => {
      try {
        if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    };
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
      void (options.onTimeout?.() ?? Promise.resolve()).finally(killClient);
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

/**
 * After a failed render: remove what studio-hf-render.sh can have left in
 * `outputDir` for `name` — `<name>` and `.partial-<name>`, each a plain file
 * or a directory holding only plain files. Anything else is left, with a note.
 */
async function cleanContainerOutput(outputDir: string, name: string): Promise<void> {
  for (const entry of [name, `.partial-${name}`]) {
    const target = path.join(outputDir, entry);
    const st = await lstat(target).catch(() => null);
    if (!st) continue;
    if (st.isFile()) {
      await unlink(target);
      continue;
    }
    if (!st.isDirectory()) {
      process.stderr.write(`note: left ${target} (not a file or directory)\n`);
      continue;
    }
    for (const child of await readdir(target, { withFileTypes: true })) {
      if (child.isFile()) await unlink(path.join(target, child.name));
    }
    await rmdir(target).catch(() => process.stderr.write(`note: left ${target} (not empty)\n`));
  }
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
  private readonly createTimeoutMs: number;
  /** Containers of this adapter whose render has not finished cleaning up. */
  private readonly running = new Set<string>();
  /** Renders in flight; close() waits for them to unwind. */
  private readonly inFlight = new Set<Promise<unknown>>();
  /** Labels every container this adapter makes (close() sweeps by it). */
  readonly adapterId = randomBytes(8).toString("hex");
  /** Set by close(); every phase checks it (single-use, see EngineAdapter). */
  private closed = false;
  private closing: Promise<void> | null = null;
  /** Whether this adapter ever ran `docker create` (else close() has nothing to sweep). */
  private createAttempted = false;

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
    this.createTimeoutMs = options.createTimeoutMs ?? CREATE_TIMEOUT_MS;
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
    ], { timeoutMs: CLEANUP_CALL_TIMEOUT_MS });
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

  private assertOpen(): void {
    if (this.closed) throw new AdapterClosed(this.name);
  }

  /**
   * close(): kill a render's container. `docker kill` fails on a container
   * that is created but not yet running, and `docker start` can be just
   * about to run it, so the kill is retried until it lands or the render
   * has stopped using the container (it leaves `running` once its cleanup
   * has run), for at most CLEANUP_CALL_TIMEOUT_MS.
   */
  private async stopContainer(name: string): Promise<void> {
    const deadline = Date.now() + CLEANUP_CALL_TIMEOUT_MS;
    while (this.running.has(name) && Date.now() < deadline) {
      const result = await run(this.docker, ["kill", "--", name], {
        timeoutMs: Math.max(1, deadline - Date.now()),
      }).catch(() => null);
      if (result === null || result.code === 0) return;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }

  private async killContainer(name: string): Promise<void> {
    await run(this.docker, ["kill", "--", name], { timeoutMs: CLEANUP_CALL_TIMEOUT_MS }).catch(() => undefined);
  }

  /**
   * Remove every container carrying `label` (and the one named `name`, if
   * given) until none is left, with `docker rm -fv` ("No such container" and
   * "removal in progress" are expected and ignored).
   *
   * - `lateCreate`: a `docker create` did not finish cleanly (killed, timed
   *   out, failed or spawn error). The daemon can still complete it after the
   *   client is gone, so the sweep keeps looking for LATE_CREATE_WINDOW_MS
   *   even while it finds nothing, instead of trusting one empty listing.
   * - Every docker call is bounded (CLEANUP_CALL_TIMEOUT_MS) and so is the
   *   whole sweep (SWEEP_MAX_MS).
   */
  private async sweep(label: string, name: string | null, lateCreate: boolean): Promise<void> {
    const started = Date.now();
    const deadline = started + SWEEP_MAX_MS;
    // Each docker call is cut at CLEANUP_CALL_TIMEOUT_MS or at the sweep's
    // deadline, whichever comes first, so the whole sweep ends by SWEEP_MAX_MS.
    const bounded = () => ({ timeoutMs: Math.max(1, Math.min(CLEANUP_CALL_TIMEOUT_MS, deadline - Date.now())) });
    const giveUp = () =>
      process.stderr.write(
        `note: could not confirm that every container labelled ${label} is gone; ` +
          `check with \`docker ps -a --filter label=${label}\`\n`,
      );
    for (;;) {
      const listed = await run(this.docker, ["ps", "-aq", "--no-trunc", "--filter", `label=${label}`], bounded()).catch(
        () => null,
      );
      // The docker CLI cannot even be started: nothing of ours can exist.
      if (listed === null) return;
      const ids = listed.code === 0 && !listed.timedOut ? listed.output.split(/\s+/).filter(Boolean) : [];
      const targets = name ? [...ids, name] : ids;
      if (targets.length && Date.now() < deadline) {
        await run(this.docker, ["rm", "-fv", "--", ...targets], bounded()).catch(() => undefined);
      }
      let named = false;
      if (name && Date.now() < deadline) {
        const inspected = await run(this.docker, ["container", "inspect", "--format", "{{.Id}}", "--", name], bounded()).catch(
          () => null,
        );
        named = inspected !== null && inspected.code === 0;
      } else if (name) {
        named = true; // not checked before the deadline: unconfirmed
      }
      const elapsed = Date.now() - started;
      const clean = listed.code === 0 && !listed.timedOut && ids.length === 0 && !named;
      if (clean && (!lateCreate || elapsed >= LATE_CREATE_WINDOW_MS)) return;
      if (Date.now() + 250 >= deadline) {
        giveUp();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
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
    this.assertOpen();
    const imageId = await this.imageId();
    this.assertOpen();
    // Refuse unmountable paths before writing anything.
    const fontconfigMount = bindMount(FONTCONFIG_DIR, "/studio-fontconfig", true);
    const outputMount = bindMount(outputDir, "/output", false);
    const projectDir = await mkdtemp(path.join(tmpdir(), "studio-hyperframes-"));
    const runId = randomBytes(8).toString("hex");
    const name = `studio-hf-${runId}`;
    let rendered = false;
    // Until `docker create` has exited 0, a container may still appear.
    let lateCreate = false;
    try {
      const projectMount = bindMount(projectDir, "/project", true);
      await writeProject(projectDir, spec);
      this.assertOpen();
      const uid = process.getuid?.();
      const gid = process.getgid?.();
      const createArgs = [
        "create",
        "--rm",
        "--init",
        "--name",
        name,
        "--label",
        `${RUN_LABEL}=${runId}`,
        "--label",
        `${ADAPTER_LABEL}=${this.adapterId}`,
        "--network",
        "none",
        "--shm-size=2g",
        "--read-only",
        "--tmpfs",
        `/tmp:rw,nosuid,nodev,size=${limit("tmpfs")}`,
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
        // The container's own deadline (docker/hyperframes/studio-hf-render.sh):
        // it ends even if nothing is left to stop it.
        "-e",
        `STUDIO_HF_DEADLINE_SECONDS=${containerDeadlineSeconds(this.timeoutMs)}`,
        "--mount",
        fontconfigMount,
        "--mount",
        projectMount,
        "--mount",
        outputMount,
        imageId,
        outputName,
        "/project",
        "--format",
        format,
        "--fps",
        String(spec.fps),
        "--quiet",
        ...(this.workers !== undefined ? ["--workers", String(this.workers)] : []),
      ];
      // Create, then start attached, as two client calls. A timeout can then
      // only interrupt `start`, after `create` has completed, so the
      // container always exists under its name when it is killed and removed.
      // (Killing a `docker run` client while the daemon was still creating
      // the container left a created, never-started container behind.)
      this.running.add(name);
      lateCreate = true;
      this.createAttempted = true;
      const created = await run(this.docker, createArgs, { timeoutMs: this.createTimeoutMs });
      if (created.timedOut || created.code !== 0) {
        throw new HyperFramesRenderError(`could not create the HyperFrames container: ${created.output.slice(-2000)}`);
      }
      lateCreate = false;
      this.assertOpen();
      const result = await run(this.docker, ["start", "--attach", "--", name], {
        log: this.log,
        onTimeout: () => this.killContainer(name),
        timeoutMs: this.timeoutMs,
      });
      if (this.closed) throw new AdapterClosed(this.name);
      if (result.timedOut) {
        throw new HyperFramesRenderError(`HyperFrames render timed out after ${this.timeoutMs} ms; container ${name} was killed`);
      }
      if (result.code !== 0) {
        throw new HyperFramesRenderError(`HyperFrames render failed (exit ${result.code}):\n${result.output.slice(-4000)}`);
      }
      rendered = true;
    } finally {
      // The client has exited (or never started). Remove this render's
      // container by name and label; after an unclean create, keep looking
      // for one the daemon may still finish creating.
      await this.sweep(`${RUN_LABEL}=${runId}`, name, lateCreate);
      this.running.delete(name);
      if (!rendered) await cleanContainerOutput(outputDir, outputName);
      await removeProject(projectDir);
    }
  }


  render(request: RenderRequest): Promise<RenderResult> {
    return this.track(this.renderUntracked(request));
  }

  private async renderUntracked(request: RenderRequest): Promise<RenderResult> {
    this.assertOpen();
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
        this.assertOpen();
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
      this.assertOpen();
      return result([await publishFile(path.join(staging, name), outPath, overwrite)]);
    });
  }

  /**
   * Chosen frames in ONE container and one composition whose i-th frame is
   * the i-th requested frame (the HyperFrames CLI has no single-frame mode).
   */
  renderStills(request: StillsRequest): Promise<string[]> {
    return this.track(this.renderStillsUntracked(request));
  }

  private async renderStillsUntracked(request: StillsRequest): Promise<string[]> {
    this.assertOpen();
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
        this.assertOpen();
        files.push(await publishFile(staged[index] as string, target(frame), overwrite));
      }
      return files;
    });
  }

  /**
   * Single-use (see EngineAdapter). Each render is its own container, so
   * close() stops what is in flight: it marks the adapter closed (a render
   * then stops at its next phase and publishes nothing), kills running
   * containers, waits for every render to unwind through its own cleanup,
   * then removes any container still carrying this adapter's label.
   * Idempotent.
   */
  async close(): Promise<void> {
    this.closed = true;
    this.closing ??= (async () => {
      await Promise.all([...this.running].map((name) => this.stopContainer(name)));
      await Promise.allSettled([...this.inFlight]);
      if (this.createAttempted) await this.sweep(`${ADAPTER_LABEL}=${this.adapterId}`, null, false);
    })();
    return this.closing;
  }
}
