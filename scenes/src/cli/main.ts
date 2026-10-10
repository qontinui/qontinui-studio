/**
 * studio-scenes — render and validate studio scenes.
 *
 *   studio-scenes render <scene> --trace <file> --format mp4|webm|gif|png
 *                        --aspect 16:9|9:16|1:1 --out <path> [--frame <n>] [--scale <s>]
 *   studio-scenes validate <trace-file> [--scene <scene>]
 *   studio-scenes list
 *
 * png: with --frame, a single still written to --out (a file); without it, the
 * whole scene as a PNG frame sequence written into --out (a directory).
 */
import { readFile } from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";

import { OUTPUT_FORMATS, type OutputFormat } from "../api/engine";
import { ASPECTS, isAspect } from "../api/layout";
import { missingRequirements } from "../api/scene";
import { validateTrace, type SceneTrace } from "../api/trace/schema";
import { getScene, scenes } from "../scenes";

const USAGE = `usage:
  studio-scenes render <scene> --trace <file> --format ${OUTPUT_FORMATS.join("|")} --aspect ${ASPECTS.join("|")} --out <path> [--frame <n>] [--scale <s>]
  studio-scenes validate <trace-file> [--scene <scene>]
  studio-scenes list

png: with --frame, one still at --out (file); without it, a PNG frame sequence in --out (directory).`;

class UsageError extends Error {}

function requireScene(id: string) {
  if (!scenes[id]) {
    throw new UsageError(`unknown scene "${id}" (known: ${Object.keys(scenes).join(", ")})`);
  }
  return getScene(id);
}

async function loadTrace(file: string): Promise<SceneTrace> {
  let json: unknown;
  try {
    json = JSON.parse(await readFile(file, "utf8"));
  } catch (err) {
    throw new UsageError(`cannot read trace ${file}: ${err instanceof Error ? err.message : String(err)}`);
  }
  const result = validateTrace(json);
  if (!result.ok) {
    throw new UsageError(`invalid trace ${file}:\n  ${result.errors.join("\n  ")}`);
  }
  return result.trace;
}

function intOption(name: string, value: string | undefined): number | undefined {
  if (value === undefined) return undefined;
  if (!/^\d+$/.test(value)) throw new UsageError(`--${name} must be a non-negative integer`);
  return Number(value);
}

async function render(args: string[]) {
  const { values, positionals } = parseArgs({
    args,
    allowPositionals: true,
    options: {
      trace: { type: "string" },
      format: { type: "string" },
      aspect: { type: "string" },
      out: { type: "string" },
      frame: { type: "string" },
      scale: { type: "string" },
    },
  });
  const [sceneId] = positionals;
  if (!sceneId) throw new UsageError("render: missing <scene>");
  if (!values.trace) throw new UsageError("render: missing --trace");
  if (!values.out) throw new UsageError("render: missing --out");
  const format = values.format ?? "";
  if (!(OUTPUT_FORMATS as readonly string[]).includes(format)) {
    throw new UsageError(`render: --format must be one of ${OUTPUT_FORMATS.join(", ")}`);
  }
  const aspect = values.aspect ?? "";
  if (!isAspect(aspect)) throw new UsageError(`render: --aspect must be one of ${ASPECTS.join(", ")}`);
  const frame = intOption("frame", values.frame);
  if (frame !== undefined && format !== "png") throw new UsageError("render: --frame is only valid with --format png");
  const scale = values.scale === undefined ? undefined : Number(values.scale);
  if (scale !== undefined && !(scale > 0 && scale <= 4)) throw new UsageError("render: --scale must be in (0, 4]");

  requireScene(sceneId);
  const trace = await loadTrace(values.trace);
  const { RemotionAdapter } = await import("../adapters/remotion/render");
  const adapter = new RemotionAdapter();
  const outPath = path.resolve(values.out);
  let lastPct = -1;
  try {
    const result = await adapter.render({
      scene: sceneId,
      trace,
      format: format as OutputFormat,
      aspect,
      outPath,
      frame,
      scale,
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
  } finally {
    await adapter.close();
  }
}

async function validate(args: string[]) {
  const { values, positionals } = parseArgs({
    args,
    allowPositionals: true,
    options: { scene: { type: "string" } },
  });
  const [file] = positionals;
  if (!file) throw new UsageError("validate: missing <trace-file>");
  const trace = await loadTrace(file);
  if (values.scene) {
    const missing = missingRequirements(requireScene(values.scene), trace);
    if (missing.length) throw new UsageError(`trace cannot drive ${values.scene}; missing: ${missing.join(", ")}`);
  }
  process.stdout.write(`ok: ${file} (trace_version ${trace.trace_version}, ${trace.meta.mode}, ${trace.events.length} events)\n`);
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
  if (err instanceof UsageError) {
    process.stderr.write(`${err.message}\n\n${USAGE}\n`);
    process.exit(2);
  }
  process.stderr.write(`${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n`);
  process.exit(1);
});
