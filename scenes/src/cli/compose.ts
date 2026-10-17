/**
 * `studio-scenes compose` — render a composition (plan Phase 5): the teaser,
 * the explainer or the factory pulse, from scenes, optional b-roll and an
 * optional narration track.
 *
 * Beside the video it writes `<out>.composition.json`, the manifest the
 * publish gate reads (what each segment is and where it sits, what was
 * omitted and why, the SHA-256 of every input), and — when the narration
 * carries aligned words — `<out>.vtt`, the captions.
 */
import { access, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { captionCues, toWebVtt } from "../api/captions";
import {
  CompositionError,
  programOutline,
  resolveProgram,
  type BrollBinding,
  type NarrationBinding,
} from "../api/composition";
import { COMPOSITION_FORMATS, type CompositionFormat } from "../api/engine";
import { ASPECTS, isAspect } from "../api/layout";
import { validateTiming, type NarrationTiming } from "../api/narration-timing";
import { missingRequirements } from "../api/scene";
import { fromBuildRecord, type BuildRecordExport } from "../api/trace/from-build-record";
import { renderRefusal } from "../api/trace/provenance";
import { validateTrace, type SceneTrace } from "../api/trace/schema";
import { compositions, getComposition } from "../compositions";
import { mediaName, sha256 } from "../gate/hash";
import { scenes } from "../scenes";
import { loadAdapter, parse, readJson } from "./common";
import { UsageError, runInterruptible } from "./interruptible";

const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

export const COMPOSE_USAGE = `  studio-scenes compose <composition> --format ${COMPOSITION_FORMATS.join("|")} --aspect ${ASPECTS.join("|")} --out <file>
                       [--trace <segment>=<file>]... | [--build-record <export>]
                       [--broll <slot>=<image>]... [--narration <timing.json>]
                       [--var <name>=<value>]... [--force]
  studio-scenes compositions

compose: scene segments use their scene's illustrative trace unless --trace names
another; --build-record makes every scene segment a replay (exits 5 while replays
are unavailable, so a caller can fall back to illustrative). A b-roll slot with no
--broll image is left out. --broll needs <image>.provenance.json beside the image.
Writes <out>, <out>.composition.json and, when narrated, <out>.vtt.`;

/** `compose --build-record` could not build a replay: exit 5, distinct from usage (2). */
export class ReplayUnavailable extends Error {}

export const MANIFEST_VERSION = 1;


/** `name=value` pairs, each name at most once. */
function pairs(flag: string, values: readonly string[] | undefined): Map<string, string> {
  const out = new Map<string, string>();
  for (const raw of values ?? []) {
    const eq = raw.indexOf("=");
    if (eq <= 0) throw new UsageError(`compose: --${flag} takes <name>=<value>, got "${raw}"`);
    const name = raw.slice(0, eq);
    if (out.has(name)) throw new UsageError(`compose: --${flag} ${name} is given twice`);
    out.set(name, raw.slice(eq + 1));
  }
  return out;
}


const IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".webp"]);
const AUDIO_EXT = new Set([".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"]);

/**
 * The b-roll image for a slot and its provenance sidecar (the studio's
 * `<file>.provenance.json`): it must say the image is AI-generated, name its
 * provider, and record this file's SHA-256.
 */
async function loadBroll(slot: string, file: string) {
  if (!IMAGE_EXT.has(path.extname(file).toLowerCase())) {
    throw new UsageError(`compose: --broll ${slot}: ${file} is not a png, jpeg or webp image`);
  }
  let bytes: Buffer;
  try {
    bytes = await readFile(file);
  } catch (err) {
    throw new UsageError(`compose: --broll ${slot}: cannot read ${file}: ${err instanceof Error ? err.message : String(err)}`);
  }
  const sidecar = `${file}.provenance.json`;
  const record = (await readJson(sidecar, `b-roll provenance (required beside every b-roll image)`)) as Record<string, unknown>;
  const provider = record.provider as Record<string, unknown> | undefined;
  const recorded = (record.file as Record<string, unknown> | undefined)?.sha256;
  const digest = sha256(bytes);
  const problems: string[] = [];
  if (record.ai_generated !== true) problems.push("ai_generated is not true");
  if (!provider || typeof provider.id !== "string" || !provider.id) problems.push("provider.id is missing");
  if (recorded !== digest) problems.push(`file.sha256 ${String(recorded)} is not the image's ${digest}`);
  if (problems.length) throw new UsageError(`compose: --broll ${slot}: ${sidecar}: ${problems.join("; ")}`);
  return { bytes, digest, provider: (provider as Record<string, unknown>).id as string, sidecar };
}

/** A JSON file and the SHA-256 of exactly the bytes that were parsed. */
async function readHashedJson(file: string, what: string): Promise<{ value: unknown; sha256: string }> {
  let bytes: Buffer;
  try {
    bytes = await readFile(file);
  } catch (err) {
    throw new UsageError(`cannot read ${what} ${file}: ${err instanceof Error ? err.message : String(err)}`);
  }
  try {
    return { value: JSON.parse(bytes.toString("utf8")), sha256: sha256(bytes) };
  } catch (err) {
    throw new UsageError(`cannot read ${what} ${file}: ${err instanceof Error ? err.message : String(err)}`);
  }
}

async function loadTiming(file: string): Promise<{ timing: NarrationTiming; sha256: string }> {
  const { value, sha256: digest } = await readHashedJson(file, "narration timing table");
  const result = validateTiming(value);
  if (!result.ok) throw new UsageError(`compose: invalid narration timing ${file}:\n  ${result.errors.join("\n  ")}`);
  return { timing: result.timing, sha256: digest };
}

async function refuseExisting(file: string, force: boolean): Promise<void> {
  if (force) return;
  try {
    await access(file);
  } catch {
    return;
  }
  throw new UsageError(`compose: refusing to overwrite ${file}: it exists (pass --force to replace it)`);
}

/** Write a small text file atomically beside the output. */
async function writeBeside(file: string, content: string): Promise<void> {
  const staged = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.tmp`);
  await writeFile(staged, content, { flag: "wx" });
  await rename(staged, file);
}

export async function compose(args: string[]): Promise<void> {
  const { values, positionals } = parse({
    args,
    allowPositionals: true,
    options: {
      format: { type: "string" },
      aspect: { type: "string" },
      out: { type: "string" },
      trace: { type: "string", multiple: true },
      "build-record": { type: "string" },
      broll: { type: "string", multiple: true },
      narration: { type: "string" },
      var: { type: "string", multiple: true },
      force: { type: "boolean", default: false },
    },
  });
  const [compositionId, ...extra] = positionals;
  if (!compositionId) throw new UsageError("compose: missing <composition>");
  if (extra.length) throw new UsageError(`compose: unexpected argument "${extra[0]}"`);
  if (!compositions[compositionId]) {
    throw new UsageError(`compose: unknown composition "${compositionId}" (known: ${Object.keys(compositions).join(", ")})`);
  }
  const definition = getComposition(compositionId);
  const format = (values.format as string | undefined) ?? "";
  if (!(COMPOSITION_FORMATS as readonly string[]).includes(format)) {
    throw new UsageError(`compose: --format must be one of ${COMPOSITION_FORMATS.join(", ")}`);
  }
  const aspect = (values.aspect as string | undefined) ?? "";
  if (!isAspect(aspect)) throw new UsageError(`compose: --aspect must be one of ${ASPECTS.join(", ")}`);
  const out = values.out as string | undefined;
  if (!out) throw new UsageError("compose: missing --out");
  const outPath = path.resolve(out);
  const force = values.force === true;
  const manifestPath = `${outPath}.composition.json`;
  const captionsPath = `${outPath}.vtt`;

  const traceFiles = pairs("trace", values.trace as string[] | undefined);
  const buildRecordFile = values["build-record"] as string | undefined;
  if (buildRecordFile !== undefined && traceFiles.size) {
    throw new UsageError("compose: give --trace or --build-record, not both");
  }
  const sceneSegments = definition.segments.flatMap((s) => (s.kind === "scene" ? [s] : []));
  for (const id of traceFiles.keys()) {
    if (!sceneSegments.some((s) => s.id === id)) {
      throw new UsageError(`compose: --trace ${id}: ${compositionId} has no scene segment "${id}" (scene segments: ${sceneSegments.map((s) => s.id).join(", ")})`);
    }
  }

  // Traces: one per scene segment.
  const traces: Record<string, SceneTrace> = {};
  const traceInputs: { segment: string; scene: string; mode: string; source: "trace" | "build-record"; file: string; sha256: string }[] = [];
  let record: { value: BuildRecordExport; sha256: string } | null = null;
  if (buildRecordFile !== undefined) {
    const read = await readHashedJson(buildRecordFile, "build-record export");
    record = { value: read.value as BuildRecordExport, sha256: read.sha256 };
  }
  for (const segment of sceneSegments) {
    const scene = scenes[segment.scene];
    if (!scene) throw new UsageError(`compose: ${compositionId}/${segment.id}: unknown scene "${segment.scene}"`);
    let trace: SceneTrace;
    let file: string;
    let digest: string;
    if (record) {
      try {
        trace = fromBuildRecord(record.value, { sourceRef: buildRecordFile as string, title: scene.title });
      } catch (err) {
        throw new ReplayUnavailable(`compose: --build-record: ${err instanceof Error ? err.message : String(err)}`);
      }
      file = path.resolve(buildRecordFile as string);
      digest = record.sha256;
    } else {
      file = path.resolve(traceFiles.get(segment.id) ?? path.join(PACKAGE_ROOT, "traces/illustrative", `${segment.scene}.json`));
      const read = await readHashedJson(file, "trace");
      const result = validateTrace(read.value);
      if (!result.ok) throw new UsageError(`compose: invalid trace ${file}:\n  ${result.errors.join("\n  ")}`);
      trace = result.trace;
      digest = read.sha256;
    }
    const refusal = renderRefusal(trace);
    if (refusal) throw new UsageError(`compose: ${segment.id}: ${refusal}`);
    const problems = missingRequirements(scene, trace);
    if (problems.length) throw new UsageError(`compose: ${segment.id}: trace cannot drive ${scene.id}; it needs: ${problems.join("; ")}`);
    traces[segment.id] = trace;
    traceInputs.push({ segment: segment.id, scene: scene.id, mode: trace.meta.mode, source: record ? "build-record" : "trace", file, sha256: digest });
  }

  // Media the adapter will serve: b-roll and narration clips.
  const media: Record<string, string> = {};
  const broll: Record<string, BrollBinding> = {};
  const brollInputs: { slot: string; file: string; sha256: string; provider: string; provenance: string }[] = [];
  const slots = definition.segments.flatMap((s) => (s.kind === "broll" ? [s.slot] : []));
  for (const [slot, file] of pairs("broll", values.broll as string[] | undefined)) {
    if (!slots.includes(slot)) {
      throw new UsageError(`compose: b-roll slot "${slot}" is not a slot of ${compositionId} (slots: ${slots.join(", ") || "none"})`);
    }
    const loaded = await loadBroll(slot, file);
    const name = mediaName(loaded.bytes, file);
    media[name] = path.resolve(file);
    broll[slot] = { src: name };
    brollInputs.push({ slot, file: path.resolve(file), sha256: loaded.digest, provider: loaded.provider, provenance: path.resolve(loaded.sidecar) });
  }

  const narrationFile = values.narration as string | undefined;
  let timing: NarrationTiming | null = null;
  let timingSha256 = "";
  const narration: Record<string, NarrationBinding> = {};
  if (narrationFile !== undefined) {
    ({ timing, sha256: timingSha256 } = await loadTiming(narrationFile));
    if (timing.composition !== compositionId) {
      throw new UsageError(`compose: --narration ${narrationFile} was voiced for "${timing.composition}", not "${compositionId}"`);
    }
    const dir = path.dirname(path.resolve(narrationFile));
    for (const [segmentId, clip] of Object.entries(timing.segments)) {
      const file = path.join(dir, clip.audio);
      if (!AUDIO_EXT.has(path.extname(file).toLowerCase())) throw new UsageError(`compose: narration ${segmentId}: ${clip.audio} is not an audio file`);
      let bytes: Buffer;
      try {
        bytes = await readFile(file);
      } catch (err) {
        throw new UsageError(`compose: narration ${segmentId}: cannot read ${file}: ${err instanceof Error ? err.message : String(err)}`);
      }
      if (sha256(bytes) !== clip.audio_sha256) {
        throw new UsageError(`compose: narration ${segmentId}: ${clip.audio} does not hash to the table's audio_sha256 (re-voice, or restore the clip)`);
      }
      const name = mediaName(bytes, file);
      media[name] = file;
      narration[segmentId] = { src: name, narration: clip };
    }
  }

  const variables = Object.fromEntries(pairs("var", values.var as string[] | undefined));
  let program;
  try {
    program = resolveProgram({ definition, scenes, traces, variables, broll, narration });
  } catch (err) {
    if (err instanceof CompositionError) throw new UsageError(`compose: ${err.message}`);
    throw err;
  }
  const captions = timing ? captionCues(program, timing) : [];
  for (const target of [manifestPath, ...(captions.length ? [captionsPath] : [])]) await refuseExisting(target, force);

  const adapter = await loadAdapter("remotion");
  if (!adapter.renderComposition) throw new UsageError(`compose: the ${adapter.name} adapter cannot compose`);
  const renderComposition = adapter.renderComposition.bind(adapter);
  let lastPct = -1;
  if (!captions.length) {
    try {
      await access(captionsPath);
      process.stderr.write(`note: ${captionsPath} is from an earlier run; this composition has no captions (the manifest says so)\n`);
    } catch {
      // No stale captions file.
    }
  }
  // The manifest and captions are written inside the interruptible work, so
  // a signal can never leave a published video without them.
  const result = await runInterruptible(adapter, outPath, "compose", async () => {
    const rendered = await renderComposition({
      program,
      format: format as CompositionFormat,
      aspect,
      outPath,
      media,
      overwrite: force,
      onProgress: (fraction) => {
        const pct = Math.floor(fraction * 100);
        if (pct >= lastPct + 10) {
          lastPct = pct;
          process.stderr.write(`  ${pct}%\n`);
        }
      },
    });
    if (captions.length) await writeBeside(captionsPath, toWebVtt(captions));
    const manifest = {
      manifest_version: MANIFEST_VERSION,
      composition: compositionId,
      title: definition.title,
      aspect,
      format,
      fps: rendered.fps,
      width: rendered.width,
      height: rendered.height,
      duration_in_frames: rendered.durationInFrames,
      output: { file: path.basename(outPath), sha256: sha256(await readFile(outPath)) },
      captions: captions.length ? { file: path.basename(captionsPath), cues: captions.length } : null,
      variables,
      segments: programOutline(program),
      omitted: program.omitted,
      notes: program.notes,
      traces: traceInputs,
      broll: brollInputs,
      narration: timing
        ? {
            file: path.resolve(narrationFile as string),
            sha256: timingSha256,
            voice: timing.voice,
            script_sha256: timing.script_sha256,
            clips: program.audio.map((clip) => ({
              segment: clip.segmentId,
              start_frame: clip.startFrame,
              duration_in_frames: clip.durationInFrames,
              audio_sha256: (timing as NarrationTiming).segments[clip.segmentId]?.audio_sha256 ?? null,
              speech: (timing as NarrationTiming).segments[clip.segmentId]?.speech ?? null,
            })),
          }
        : null,
      engine: adapter.name,
    };
    await writeBeside(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
    return rendered;
  });
  process.stdout.write(
    `${outPath}\n  ${result.width}x${result.height} @ ${result.fps} fps, ${result.durationInFrames} frames, ` +
      `${program.segments.length} segments (${program.omitted.length} omitted), ${program.audio.length} narration clips\n` +
      `${manifestPath}\n${captions.length ? `${captionsPath}\n` : ""}`,
  );
  for (const note of [...program.omitted.map((o) => `omitted ${o.id}: ${o.reason}`), ...program.notes]) {
    process.stderr.write(`note: ${note}\n`);
  }
}

export function listCompositions(): void {
  for (const composition of Object.values(compositions)) {
    process.stdout.write(`${composition.id}\t${composition.title}\n`);
  }
}
