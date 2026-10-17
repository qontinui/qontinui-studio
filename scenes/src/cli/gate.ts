/**
 * `studio-scenes gate <video>` — the unattended publish gate (plan Phase 5).
 * Writes `<video>.gate.json` (every check, why it passed or failed) and
 * exits 0 when the video may be published, 3 when it may not.
 */
import { rename, writeFile } from "node:fs/promises";
import path from "node:path";

import { DEFAULT_THRESHOLDS, type GateThresholds } from "../gate/checks";
import { runGate, type LayoutInspector } from "../gate/gate";
import { parse } from "./common";
import { UsageError } from "./interruptible";

export const GATE_USAGE = `  studio-scenes gate <video> [--min-speech <0..1>] [--max-silence <s>]

gate: reads <video>.composition.json (written by compose), runs every publish check
(manifest, speech, layout, ffprobe, frames, audio, captions) and writes <video>.gate.json.
Exits 0 when the video may be published, 3 when it may not (the report says why).
ffprobe/ffmpeg come from STUDIO_FFPROBE / STUDIO_FFMPEG, else PATH.`;

/** The gate refused the video: exit 3. */
export class GateFailed extends Error {}

export async function gateCommand(args: string[]): Promise<void> {
  const { values, positionals } = parse({
    args,
    allowPositionals: true,
    options: { "min-speech": { type: "string" }, "max-silence": { type: "string" } },
  });
  const [video, ...extra] = positionals;
  if (!video) throw new UsageError("gate: missing <video>");
  if (extra.length) throw new UsageError(`gate: unexpected argument "${extra[0]}"`);
  const thresholds: GateThresholds = { ...DEFAULT_THRESHOLDS };
  if (values["min-speech"] !== undefined) {
    const v = Number(values["min-speech"]);
    if (!(v >= 0 && v <= 1)) throw new UsageError("gate: --min-speech must be within [0, 1]");
    thresholds.minSpeechMatch = v;
  }
  if (values["max-silence"] !== undefined) {
    const v = Number(values["max-silence"]);
    if (!(v > 0 && v <= 600)) throw new UsageError("gate: --max-silence must be within (0, 600] seconds");
    thresholds.maxSilenceSeconds = v;
  }
  const inspectLayout: LayoutInspector = async (program, aspect, frames, images) => {
    const { inspectProgramLayout } = await import("../adapters/remotion/inspect");
    return inspectProgramLayout(program, aspect, frames, images);
  };
  const verdict = await runGate(video, { inspectLayout }, thresholds);
  const report = `${path.resolve(video)}.gate.json`;
  const staged = path.join(path.dirname(report), `.${path.basename(report)}.${process.pid}.tmp`);
  await writeFile(staged, `${JSON.stringify(verdict, null, 2)}\n`, { flag: "wx" });
  await rename(staged, report);
  for (const check of verdict.checks) {
    process.stdout.write(`${check.status === "pass" ? "PASS" : "FAIL"}  ${check.name}: ${check.detail}\n`);
    for (const w of check.warnings ?? []) process.stdout.write(`      warning: ${w}\n`);
  }
  process.stdout.write(`${verdict.verdict.toUpperCase()}: ${report}\n`);
  if (verdict.verdict !== "pass") throw new GateFailed(`gate: publish blocked (${verdict.failed.join(", ")}); see ${report}`);
}
