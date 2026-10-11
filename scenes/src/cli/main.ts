/**
 * studio-scenes — render and validate studio scenes.
 *
 *   studio-scenes render <scene> (--trace <file> | --build-record <export>)
 *                        --format mp4|webm|gif|png --aspect 16:9|9:16|1:1 --out <path>
 *                        [--frame <n>] [--scale <s> --allow-nondeterministic] [--force]
 *                        [--engine remotion|hyperframes]
 *   studio-scenes validate <trace-file> [--scene <scene>]
 *   studio-scenes list
 *
 * png: with --frame, a single still written to --out (a file); without it, the
 * whole scene as a PNG frame sequence written into --out (a directory).
 *
 * Replays (plan D2): a `--trace` file must be illustrative. A replay renders
 * only from `--build-record <export>` through `fromBuildRecord`, which needs
 * the sibling plan's export (Phase 3) — until then every replay is refused.
 *
 * The CLI reaches the engine adapter only through a dynamic import (lint
 * forbids static imports of src/adapters from here).
 */
import { readFile } from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";

import { OUTPUT_FORMATS, type EngineAdapter, type OutputFormat } from "../api/engine";
import { ASPECTS, STUDIO_FPS, isAspect, scaleProblem } from "../api/layout";
import { missingRequirements } from "../api/scene";
import { fromBuildRecord, type BuildRecordExport } from "../api/trace/from-build-record";
import { renderRefusal } from "../api/trace/provenance";
import { validateTrace, type SceneTrace } from "../api/trace/schema";
import { getScene, scenes } from "../scenes";

const ENGINES = ["remotion", "hyperframes"] as const;
type Engine = (typeof ENGINES)[number];

const USAGE = `usage:
  studio-scenes render <scene> (--trace <file> | --build-record <export>)
                       --format ${OUTPUT_FORMATS.join("|")} --aspect ${ASPECTS.join("|")} --out <path>
                       [--frame <n>] [--scale <s> --allow-nondeterministic] [--force]
                       [--engine ${ENGINES.join("|")}]
  studio-scenes validate <trace-file> [--scene <scene>]
  studio-scenes list

png: with --frame, one still at --out (file); without it, a PNG frame sequence in --out (directory).
--force replaces an existing output file, or a previous element-*.png sequence; nothing else.
--scale other than 1 needs --allow-nondeterministic (only scale 1 is byte-reproducible),
and must give even pixel dimensions for every aspect (e.g. 0.25, 0.5, 2).
A --trace file must be illustrative; replays come only from --build-record (plan D2).
--engine defaults to remotion; hyperframes renders in its pinned Docker image
(build it once with \`pnpm hyperframes:image\`) and supports scale 1 only.`;

class UsageError extends Error {}

/** The render was stopped by a signal; the process exits 128 + signal number. */
class Interrupted extends Error {
  constructor(
    readonly signal: NodeJS.Signals,
    outcome: string,
  ) {
    super(`interrupted by ${signal}: ${outcome}`);
  }
}

/** A signal this soon after the first is the same interrupt relayed, not a second one. */
const REPEAT_SIGNAL_MS = 1000;

function exitCodeFor(signal: NodeJS.Signals): number {
  return signal === "SIGTERM" ? 143 : 130;
}

function requireScene(id: string) {
  if (!scenes[id]) {
    throw new UsageError(`unknown scene "${id}" (known: ${Object.keys(scenes).join(", ")})`);
  }
  return getScene(id);
}

async function readJson(file: string, what: string): Promise<unknown> {
  try {
    return JSON.parse(await readFile(file, "utf8"));
  } catch (err) {
    throw new UsageError(`cannot read ${what} ${file}: ${err instanceof Error ? err.message : String(err)}`);
  }
}

async function loadTrace(file: string): Promise<SceneTrace> {
  const result = validateTrace(await readJson(file, "trace"));
  if (!result.ok) {
    throw new UsageError(`invalid trace ${file}:\n  ${result.errors.join("\n  ")}`);
  }
  return result.trace;
}

/** parseArgs with its errors (unknown option, missing value) as usage errors. */
function parse<T extends Parameters<typeof parseArgs>[0]>(config: T) {
  try {
    return parseArgs(config);
  } catch (err) {
    throw new UsageError(err instanceof Error ? err.message : String(err));
  }
}

function intOption(name: string, value: string | undefined): number | undefined {
  if (value === undefined) return undefined;
  if (!/^\d+$/.test(value)) throw new UsageError(`--${name} must be a non-negative integer`);
  return Number(value);
}

/** The adapter for `engine`, loaded only when used (lint: dynamic import only). */
async function loadAdapter(engine: Engine): Promise<EngineAdapter> {
  if (engine === "hyperframes") {
    const { HyperFramesAdapter } = await import("../adapters/hyperframes/render");
    return new HyperFramesAdapter();
  }
  const { RemotionAdapter } = await import("../adapters/remotion/render");
  // The CLI handles SIGINT itself (see render()), so Remotion's exit-at-once
  // listener is removed and an interrupted render cleans up.
  return new RemotionAdapter({ interrupts: "caller" });
}

async function render(args: string[]) {
  const { values, positionals } = parse({
    args,
    allowPositionals: true,
    options: {
      trace: { type: "string" },
      "build-record": { type: "string" },
      format: { type: "string" },
      aspect: { type: "string" },
      out: { type: "string" },
      frame: { type: "string" },
      scale: { type: "string" },
      force: { type: "boolean", default: false },
      "allow-nondeterministic": { type: "boolean", default: false },
      engine: { type: "string", default: "remotion" },
    },
  });
  const engine = values.engine as string;
  if (!(ENGINES as readonly string[]).includes(engine)) {
    throw new UsageError(`render: --engine must be one of ${ENGINES.join(", ")}`);
  }
  if (engine === "hyperframes" && values.scale !== undefined && Number(values.scale) !== 1) {
    throw new UsageError("render: --engine hyperframes renders at scale 1 only");
  }
  const [sceneId, ...extra] = positionals;
  if (!sceneId) throw new UsageError("render: missing <scene>");
  if (extra.length) throw new UsageError(`render: unexpected argument "${extra[0]}"`);
  const traceFile = values.trace as string | undefined;
  const buildRecordFile = values["build-record"] as string | undefined;
  if ((traceFile === undefined) === (buildRecordFile === undefined)) {
    throw new UsageError("render: give exactly one of --trace or --build-record");
  }
  const out = values.out as string | undefined;
  if (!out) throw new UsageError("render: missing --out");
  const format = (values.format as string | undefined) ?? "";
  if (!(OUTPUT_FORMATS as readonly string[]).includes(format)) {
    throw new UsageError(`render: --format must be one of ${OUTPUT_FORMATS.join(", ")}`);
  }
  const aspect = (values.aspect as string | undefined) ?? "";
  if (!isAspect(aspect)) throw new UsageError(`render: --aspect must be one of ${ASPECTS.join(", ")}`);
  const frame = intOption("frame", values.frame as string | undefined);
  if (frame !== undefined && format !== "png") throw new UsageError("render: --frame is only valid with --format png");
  const scaleText = values.scale as string | undefined;
  const scale = scaleText === undefined ? undefined : Number(scaleText);
  if (scale !== undefined && scale !== 1) {
    if (values["allow-nondeterministic"] !== true) {
      throw new UsageError(
        `render: --scale ${scaleText} is refused: renders are byte-reproducible only at scale 1 ` +
          "(at a fractional scale an edge pixel was seen to vary by 1 between runs). " +
          "Render at scale 1 and resize, or pass --allow-nondeterministic.",
      );
    }
    const problem = scaleProblem(scale);
    if (problem) throw new UsageError(`render: ${problem}`);
  }

  const scene = requireScene(sceneId);
  let trace: SceneTrace;
  if (buildRecordFile !== undefined) {
    const record = (await readJson(buildRecordFile, "build-record export")) as BuildRecordExport;
    try {
      trace = fromBuildRecord(record, { sourceRef: buildRecordFile, title: scene.title });
    } catch (err) {
      throw new UsageError(`render: --build-record: ${err instanceof Error ? err.message : String(err)}`);
    }
  } else {
    trace = await loadTrace(traceFile as string);
  }
  const refusal = renderRefusal(trace);
  if (refusal) throw new UsageError(`render: ${refusal}`);
  const problems = missingRequirements(scene, trace);
  if (problems.length) throw new UsageError(`render: trace cannot drive ${sceneId}; it needs: ${problems.join("; ")}`);
  if (frame !== undefined) {
    const duration = scene.timeline(trace, STUDIO_FPS).durationInFrames;
    if (frame >= duration) throw new UsageError(`render: --frame ${frame} is outside 0..${duration - 1} for this trace`);
  }

  const adapter = await loadAdapter(engine as Engine);
  const outPath = path.resolve(out);
  let lastPct = -1;
  // Ctrl-C / SIGTERM: adapter.close() stops the render (an adapter publishes
  // nothing after close), the render unwinds through its own cleanup
  // (staging and project directories, containers, exactly as on any other
  // failure), and the CLI exits 130 / 143 saying which happened: stopped
  // with nothing published, or finished before the signal took effect. A
  // second signal exits at once, skipping cleanup — but only one that comes
  // REPEAT_SIGNAL_MS or more after the first: one Ctrl-C can arrive twice
  // (a wrapper such as pnpm or the tsx CLI relays the signal the terminal
  // already sent to the whole group), and that must not skip the cleanup.
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
  process.on("SIGINT", onSignal);
  process.on("SIGTERM", onSignal);
  process.on("unhandledRejection", onStrayRejection);
  try {
    const result = await adapter.render({
      scene: sceneId,
      trace,
      format: format as OutputFormat,
      aspect,
      outPath,
      frame,
      scale,
      overwrite: values.force === true,
      onProgress: (fraction) => {
        const pct = Math.floor(fraction * 100);
        if (pct >= lastPct + 10) {
          lastPct = pct;
          process.stderr.write(`  ${pct}%\n`);
        }
      },
    });
    process.stdout.write(
      `${result.files.length === 1 ? result.files[0] : `${result.files.length} frames in ${outPath}`}\n` +
        `  ${result.width}x${result.height} @ ${result.fps} fps, ${result.durationInFrames} frames (${trace.meta.mode})\n`,
    );
    if (interrupted) {
      // The render had already published when the signal took effect.
      throw new Interrupted(interrupted, `the render had already finished; ${outPath} is complete`);
    }
  } catch (err) {
    if (err instanceof Interrupted) throw err;
    if (interrupted) {
      // AdapterClosed: close() stopped the render before it published. Any
      // other error: the render failed on its own (possibly while stopping);
      // either way publishing is the last step, so nothing reached --out.
      const what =
        err instanceof Error && err.name === "AdapterClosed"
          ? "the render was stopped"
          : `the render failed while stopping (${err instanceof Error ? err.message : String(err)})`;
      throw new Interrupted(interrupted, `${what}; nothing was published to ${outPath} and its temporary files were removed`);
    }
    if (err instanceof Error && err.name === "OutputRefused") throw new UsageError(`render: ${err.message}`);
    throw err;
  } finally {
    await adapter.close();
    process.off("SIGINT", onSignal);
    process.off("SIGTERM", onSignal);
    // After a signal it stays until the process exits (130 / 143).
    if (!interrupted) process.off("unhandledRejection", onStrayRejection);
  }
}

async function validate(args: string[]) {
  const { values, positionals } = parse({
    args,
    allowPositionals: true,
    options: { scene: { type: "string" } },
  });
  const [file] = positionals;
  if (!file) throw new UsageError("validate: missing <trace-file>");
  const trace = await loadTrace(file);
  const sceneId = values.scene as string | undefined;
  if (sceneId) {
    const problems = missingRequirements(requireScene(sceneId), trace);
    if (problems.length) throw new UsageError(`trace cannot drive ${sceneId}; it needs: ${problems.join("; ")}`);
  }
  const note = renderRefusal(trace) ? " — valid shape, but NOT renderable: replays render only via --build-record" : "";
  process.stdout.write(
    `ok: ${file} (trace_version ${trace.trace_version}, ${trace.meta.mode}, ${trace.events.length} events)${note}\n`,
  );
}

async function main(argv: string[]) {
  const [command, ...rest] = argv;
  switch (command) {
    case "render":
      return render(rest);
    case "validate":
      return validate(rest);
    case "list":
      for (const scene of Object.values(scenes)) process.stdout.write(`${scene.id}\t${scene.title}\n`);
      return;
    case undefined:
    case "help":
    case "--help":
    case "-h":
      process.stdout.write(`${USAGE}\n`);
      return;
    default:
      throw new UsageError(`unknown command "${command}"`);
  }
}

main(process.argv.slice(2)).catch((err: unknown) => {
  if (err instanceof Interrupted) {
    process.stderr.write(`${err.message}\n`);
    process.exit(exitCodeFor(err.signal));
  }
  if (err instanceof UsageError) {
    process.stderr.write(`${err.message}\n\n${USAGE}\n`);
    process.exit(2);
  }
  process.stderr.write(`${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n`);
  process.exit(1);
});
