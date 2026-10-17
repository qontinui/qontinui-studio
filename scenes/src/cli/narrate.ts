/**
 * `studio-scenes narrate` — voice a composition's narration script (plan
 * Phase 5): speak -> re-transcribe -> align, then write one clip per segment
 * and the timing table `compose --narration` reads.
 */
import { lstat, readdir, readFile, rename, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";

import { compositions, getComposition } from "../compositions";
import { NarrationError, clipName, narrate } from "../narration/narrate";
import {
  SpeechServiceError,
  kokoroTts,
  speechConfigFromEnv,
  whisperAsr,
  type AsrClient,
} from "../narration/services";
import { parse } from "./common";
import { UsageError } from "./interruptible";

export const NARRATE_USAGE = `  studio-scenes narrate <composition> --script <file.md> --out <dir>
                       [--speed <s>] [--voice <v>] [--tts-url <url>] [--asr-url <url>] [--asr-model <m>] [--force]

narrate: the script has one "## <segment-id>" section per narrated segment, with
[#mark] / [pause s] / {shown|spoken} cues. TTS is Kokoro-FastAPI (STUDIO_TTS_URL),
the check is an OpenAI-compatible Whisper endpoint (STUDIO_ASR_URL); keys, if any,
come from files named by STUDIO_TTS_API_KEY_FILE / STUDIO_ASR_API_KEY_FILE.
Writes <dir>/<segment>.wav and <dir>/<composition>.timing.json. Exits 6 when the
TTS service is not configured or fails (nothing is written then).`;

/** No usable TTS service: exit 6, so a scheduled caller can render without narration and say why. */
export class NarrationUnavailable extends Error {}

export const timingFileName = (compositionId: string) => `${compositionId}.timing.json`;

async function exists(file: string): Promise<boolean> {
  try {
    await lstat(file);
    return true;
  } catch {
    return false;
  }
}

/** Write `bytes` to `file` through a temporary file beside it. */
async function put(file: string, bytes: Buffer | string): Promise<void> {
  const staged = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.tmp`);
  await writeFile(staged, bytes, { flag: "wx" });
  await rename(staged, file);
}

export async function narrateCommand(args: string[]): Promise<void> {
  const { values, positionals } = parse({
    args,
    allowPositionals: true,
    options: {
      script: { type: "string" },
      out: { type: "string" },
      speed: { type: "string" },
      voice: { type: "string" },
      "tts-url": { type: "string" },
      "asr-url": { type: "string" },
      "asr-model": { type: "string" },
      force: { type: "boolean", default: false },
    },
  });
  const [compositionId, ...extra] = positionals;
  if (!compositionId) throw new UsageError("narrate: missing <composition>");
  if (extra.length) throw new UsageError(`narrate: unexpected argument "${extra[0]}"`);
  if (!compositions[compositionId]) {
    throw new UsageError(`narrate: unknown composition "${compositionId}" (known: ${Object.keys(compositions).join(", ")})`);
  }
  const definition = getComposition(compositionId);
  const scriptFile = values.script as string | undefined;
  if (!scriptFile) throw new UsageError("narrate: missing --script");
  const out = values.out as string | undefined;
  if (!out) throw new UsageError("narrate: missing --out");
  const outDir = path.resolve(out);
  const speedText = values.speed as string | undefined;
  const speed = speedText === undefined ? 1 : Number(speedText);
  if (!(speed >= 0.5 && speed <= 2)) throw new UsageError("narrate: --speed must be within [0.5, 2]");
  let script: string;
  try {
    script = await readFile(scriptFile, "utf8");
  } catch (err) {
    throw new UsageError(`narrate: cannot read --script ${scriptFile}: ${err instanceof Error ? err.message : String(err)}`);
  }

  const config = speechConfigFromEnv();
  if (values["tts-url"] !== undefined) config.ttsUrl = values["tts-url"] as string;
  if (values["asr-url"] !== undefined) config.asrUrl = values["asr-url"] as string;
  if (values.voice !== undefined) config.ttsVoice = values.voice as string;
  if (values["asr-model"] !== undefined) config.asrModel = values["asr-model"] as string;

  // The output directory: absent, empty, or holding only what this command writes.
  const tableFile = path.join(outDir, timingFileName(compositionId));
  if (await exists(outDir)) {
    const entries = await readdir(outDir);
    const ours = new Set([timingFileName(compositionId), ...definition.segments.map((s) => clipName(s.id))]);
    const foreign = entries.filter((name) => !ours.has(name));
    if (foreign.length) {
      throw new UsageError(`narrate: refusing to write into ${outDir}: it holds files this command did not write (${foreign.slice(0, 5).join(", ")})`);
    }
    if (entries.length && values.force !== true) {
      throw new UsageError(`narrate: refusing to overwrite the narration in ${outDir} (pass --force to replace it)`);
    }
  }

  let tts;
  try {
    tts = kokoroTts(config);
  } catch (err) {
    throw new NarrationUnavailable(`narrate: ${err instanceof Error ? err.message : String(err)}`);
  }
  let asr: AsrClient | null = null;
  let asrUnavailableReason: string | undefined;
  try {
    asr = whisperAsr(config);
  } catch (err) {
    asrUnavailableReason = err instanceof Error ? err.message : String(err);
  }

  let result;
  try {
    result = await narrate({ definition, script, tts, asr, ...(asrUnavailableReason ? { asrUnavailableReason } : {}), speed });
  } catch (err) {
    if (err instanceof SpeechServiceError) throw new NarrationUnavailable(`narrate: ${err.message}`);
    if (err instanceof NarrationError) throw new UsageError(`narrate: ${err.message}`);
    throw err;
  }

  await mkdir(outDir, { recursive: true });
  for (const segment of result.segments) await put(path.join(outDir, segment.narration.audio), segment.wav);
  await put(tableFile, `${JSON.stringify(result.timing, null, 2)}\n`);
  process.stdout.write(`${tableFile}\n  voice: ${result.timing.voice}\n`);
  for (const segment of result.segments) {
    const speech = segment.narration.speech;
    const check = !speech
      ? "not checked"
      : speech.status === "measured"
        ? `speech match ${speech.match.toFixed(3)} (${speech.by})`
        : `speech check unavailable: ${speech.reason}`;
    process.stdout.write(`  ${segment.id}: ${segment.narration.duration_seconds.toFixed(2)} s, ${Object.keys(segment.narration.marks).length} marks; ${check}\n`);
  }
}
