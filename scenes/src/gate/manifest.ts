/**
 * The composition manifest `compose` writes beside a video
 * (`<out>.composition.json`), as the publish gate reads it, and the program
 * rebuilt from the inputs it names. The gate never trusts the manifest's own
 * account: every input is re-read and must hash to the manifest's value, and
 * the program rebuilt from them must place every segment exactly where the
 * manifest says.
 */
import { readFile } from "node:fs/promises";
import path from "node:path";

import { z } from "zod";

import { programOutline, resolveProgram, type BrollBinding, type NarrationBinding, type Program } from "../api/composition";
import { ASPECTS, type Aspect } from "../api/layout";
import { validateTiming, type NarrationTiming } from "../api/narration-timing";
import { fromBuildRecord, type BuildRecordExport } from "../api/trace/from-build-record";
import { validateTrace, type SceneTrace } from "../api/trace/schema";
import { compositions } from "../compositions";
import { scenes } from "../scenes";
import { mediaName, sha256 } from "./hash";

const digest = z.string().regex(/^[0-9a-f]{64}$/);

export const compositionManifest = z
  .object({
    manifest_version: z.literal(1),
    composition: z.string(),
    title: z.string(),
    aspect: z.enum(ASPECTS),
    format: z.enum(["mp4", "webm"]),
    fps: z.number().int().positive(),
    width: z.number().int().positive(),
    height: z.number().int().positive(),
    duration_in_frames: z.number().int().positive(),
    output: z.object({ file: z.string(), sha256: digest }),
    captions: z.object({ file: z.string(), cues: z.number().int().nonnegative() }).nullable(),
    variables: z.record(z.string()),
    segments: z.array(z.record(z.unknown())),
    omitted: z.array(z.object({ id: z.string(), reason: z.string() })),
    notes: z.array(z.string()),
    traces: z.array(
      z.object({
        segment: z.string(),
        scene: z.string(),
        mode: z.string(),
        source: z.enum(["trace", "build-record"]),
        file: z.string(),
        sha256: digest,
      }),
    ),
    broll: z.array(z.object({ slot: z.string(), file: z.string(), sha256: digest, provider: z.string(), provenance: z.string() })),
    narration: z
      .object({
        file: z.string(),
        sha256: digest,
        voice: z.string(),
        script_sha256: z.string(),
        clips: z.array(
          z.object({
            segment: z.string(),
            start_frame: z.number().int(),
            duration_in_frames: z.number().int(),
            audio_sha256: z.string().nullable(),
            speech: z.unknown(),
          }),
        ),
      })
      .nullable(),
    engine: z.string(),
  })
  .passthrough();
export type CompositionManifest = z.infer<typeof compositionManifest>;

export class ManifestMismatch extends Error {
  override readonly name = "ManifestMismatch";
}

async function hashed(file: string, expected: string, what: string): Promise<Buffer> {
  let bytes: Buffer;
  try {
    bytes = await readFile(file);
  } catch (err) {
    throw new ManifestMismatch(`${what} ${file} cannot be read: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (sha256(bytes) !== expected) throw new ManifestMismatch(`${what} ${file} no longer hashes to the manifest's sha256`);
  return bytes;
}

export interface RebuiltProgram {
  program: Program;
  aspect: Aspect;
  timing: NarrationTiming | null;
  /** b-roll media name -> local file, for the layout inspection. */
  images: Record<string, string>;
}

/** Rebuild the program the manifest describes, from its (re-hashed) inputs. */
export async function rebuildProgram(manifest: CompositionManifest): Promise<RebuiltProgram> {
  const definition = compositions[manifest.composition];
  if (!definition) throw new ManifestMismatch(`unknown composition "${manifest.composition}"`);
  const traces: Record<string, SceneTrace> = {};
  for (const input of manifest.traces) {
    const bytes = await hashed(input.file, input.sha256, `trace input for ${input.segment}:`);
    const value = JSON.parse(bytes.toString("utf8")) as unknown;
    if (input.source === "build-record") {
      const scene = scenes[input.scene];
      if (!scene) throw new ManifestMismatch(`unknown scene "${input.scene}"`);
      traces[input.segment] = fromBuildRecord(value as BuildRecordExport, { sourceRef: input.file, title: scene.title });
    } else {
      const result = validateTrace(value);
      if (!result.ok) throw new ManifestMismatch(`trace ${input.file} is no longer valid: ${result.errors[0]}`);
      traces[input.segment] = result.trace;
    }
  }
  const broll: Record<string, BrollBinding> = {};
  const images: Record<string, string> = {};
  for (const input of manifest.broll) {
    const bytes = await hashed(input.file, input.sha256, `b-roll ${input.slot}:`);
    const name = mediaName(bytes, input.file);
    broll[input.slot] = { src: name };
    images[name] = input.file;
  }
  let timing: NarrationTiming | null = null;
  const narration: Record<string, NarrationBinding> = {};
  if (manifest.narration) {
    const bytes = await hashed(manifest.narration.file, manifest.narration.sha256, "narration timing table");
    const result = validateTiming(JSON.parse(bytes.toString("utf8")));
    if (!result.ok) throw new ManifestMismatch(`narration timing table is invalid: ${result.errors[0]}`);
    timing = result.timing;
    const dir = path.dirname(manifest.narration.file);
    for (const [id, clip] of Object.entries(timing.segments)) {
      const clipBytes = await hashed(path.join(dir, clip.audio), clip.audio_sha256, `narration clip for ${id}:`);
      narration[id] = { src: mediaName(clipBytes, clip.audio), narration: clip };
    }
  }
  const program = resolveProgram({ definition, scenes, traces, variables: manifest.variables, broll, narration });
  const outline = JSON.parse(JSON.stringify(programOutline(program))) as unknown;
  if (JSON.stringify(outline) !== JSON.stringify(manifest.segments)) {
    throw new ManifestMismatch("the program rebuilt from the manifest's inputs does not place its segments where the manifest says");
  }
  if (program.durationInFrames !== manifest.duration_in_frames) {
    throw new ManifestMismatch(`the rebuilt program is ${program.durationInFrames} frames; the manifest says ${manifest.duration_in_frames}`);
  }
  return { program, aspect: manifest.aspect, timing, images };
}
