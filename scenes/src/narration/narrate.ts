/**
 * The narration loop (plan Phase 5, ported from explainroo):
 * speak -> re-transcribe -> align.
 *
 * For each `## <segment-id>` section of a narration script:
 *
 * 1. **Speak.** The section is cut into sentences (`speechChunks`); each is
 *    spoken by the TTS service and the clips are joined with the script's
 *    pauses between them into one clip per segment.
 * 2. **Align (seed).** The script's words are aligned against the TTS
 *    service's OWN word timestamps (`alignWords`, number- and name-aware), so
 *    every script word — and every `[#mark]` — gets a time in the clip.
 * 3. **Re-transcribe (check).** The clip is transcribed by a recogniser
 *    (Whisper) and its words aligned against the script again. The share of
 *    script tokens heard is the segment's speech match; the publish gate
 *    needs it to be at least 0.9. No recogniser means `unavailable`, which
 *    the gate refuses — never a pass.
 *
 * The result is a timing table (`src/api/narration-timing.ts`) and one clip
 * per segment, written beside it. Scenes are never edited: the composition
 * reads the table.
 */
import { createHash } from "node:crypto";

import type { CompositionDefinition } from "../api/composition";
import {
  CUE_NAME,
  TIMING_VERSION,
  validateTiming,
  type NarrationTiming,
  type SegmentNarration,
  type SpeechCheck,
} from "../api/narration-timing";
import { SpeechServiceError, type AsrClient, type TtsClient } from "./services";
import { alignWords, type AlignWord, type HeardWord } from "./vendor/explainroo/align.js";
import { parseScript, resolveMarks, speechChunks, type ScriptUnit } from "./vendor/explainroo/script.js";
import { WavError, concat, durationSeconds, readWav, silence, writeWav, type PcmAudio } from "./wav";

export class NarrationError extends Error {
  override readonly name = "NarrationError";
}

export interface NarrateInputs {
  definition: CompositionDefinition;
  /** The script's text (markdown with `## <segment-id>` sections). */
  script: string;
  tts: TtsClient;
  /** null: no recogniser configured; the check is recorded as unavailable. */
  asr: AsrClient | null;
  /** Why there is no recogniser, when `asr` is null. */
  asrUnavailableReason?: string;
  speed?: number;
}

export interface NarratedSegment {
  id: string;
  /** The clip, as WAV bytes. */
  wav: Buffer;
  narration: SegmentNarration;
}

export interface NarrationResult {
  timing: NarrationTiming;
  segments: NarratedSegment[];
}

/** The clip file name for a segment. */
export const clipName = (segmentId: string) => `${segmentId}.wav`;

function sha256(bytes: Uint8Array | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function wordUnits(units: readonly ScriptUnit[]): { index: number; unit: Extract<ScriptUnit, { type: "word" }> }[] {
  return units.flatMap((unit, index) => (unit.type === "word" ? [{ index, unit }] : []));
}

/** Speak one segment's units into one clip, with the TTS service's word times placed on the clip's clock. */
async function speakSegment(id: string, units: ScriptUnit[], tts: TtsClient, speed: number) {
  const chunks = speechChunks(units, { pace: speed });
  if (!chunks.length) throw new NarrationError(`## ${id}: the section has no words to speak`);
  const parts: PcmAudio[] = [];
  const heard: HeardWord[] = [];
  let cursor = 0;
  let format: PcmAudio | null = null;
  for (const chunk of chunks) {
    const spoken = await tts.speak(chunk.text, { speed });
    let audio: PcmAudio;
    try {
      audio = readWav(spoken.wav);
    } catch (err) {
      throw new SpeechServiceError(`## ${id}: the TTS service returned audio that is not 16-bit PCM WAV (${err instanceof Error ? err.message : String(err)})`);
    }
    format ??= audio;
    if (chunk.gapBefore > 0) {
      parts.push(silence(format, chunk.gapBefore));
      cursor += chunk.gapBefore;
    }
    parts.push(audio);
    for (const word of spoken.words) heard.push({ text: word.text, start: cursor + word.start, end: cursor + word.end });
    cursor += durationSeconds(audio);
  }
  let clip: PcmAudio;
  try {
    clip = concat(parts);
  } catch (err) {
    if (err instanceof WavError) throw new SpeechServiceError(`## ${id}: the TTS service returned clips in different formats (${err.message})`);
    throw err;
  }
  return { clip, heard, duration: durationSeconds(clip) };
}

export async function narrate(inputs: NarrateInputs): Promise<NarrationResult> {
  const { definition, tts, asr } = inputs;
  const speed = inputs.speed ?? 1;
  if (!(speed >= 0.5 && speed <= 2)) throw new NarrationError("speed must be within [0.5, 2]");
  let parsed;
  try {
    parsed = parseScript(inputs.script);
  } catch (err) {
    throw new NarrationError(err instanceof Error ? err.message : String(err));
  }
  const known = new Map(definition.segments.map((s) => [s.id, s]));
  for (const scene of parsed.scenes) {
    if (!known.has(scene.id)) {
      throw new NarrationError(
        `script section "## ${scene.id}" names no segment of ${definition.id} (segments: ${[...known.keys()].join(", ")})`,
      );
    }
  }

  for (const scene of parsed.scenes) {
    const marks = scene.units.flatMap((u) => (u.type === "mark" ? [u.name] : []));
    for (const name of marks) {
      if (!CUE_NAME.test(name) || name.length > 64) {
        throw new NarrationError(`## ${scene.id}: mark [#${name}] must start with a letter or digit and use [A-Za-z0-9_-], at most 64 characters`);
      }
    }
    const repeated = marks.find((name, i) => marks.indexOf(name) !== i);
    if (repeated) throw new NarrationError(`## ${scene.id}: mark [#${repeated}] is used twice`);
    if (!wordUnits(scene.units).length) throw new NarrationError(`## ${scene.id}: the section has no words to speak`);
  }

  const segments: NarratedSegment[] = [];
  for (const scene of parsed.scenes) {
    const words = wordUnits(scene.units);
    const { clip, heard, duration } = await speakSegment(scene.id, scene.units, tts, speed);
    const script: AlignWord[] = words.map(({ unit }) => ({ spoken: unit.spoken, display: unit.display }));

    // Seed: the TTS service's own timestamps.
    const seeded = alignWords(script, heard, duration);
    const timesByUnit: ({ start: number; end: number } | null)[] = scene.units.map(() => null);
    words.forEach(({ index }, i) => {
      timesByUnit[index] = seeded.words[i] ?? null;
    });
    const marks = resolveMarks(scene.units, timesByUnit);

    const wav = writeWav(clip);
    // Check: an independent recogniser hears the clip.
    let speech: SpeechCheck;
    if (asr) {
      try {
        const transcript = await asr.transcribe(wav);
        const checked = alignWords(script, transcript.words, duration);
        speech = {
          status: "measured",
          by: asr.describe,
          // Floored, so rounding can never lift a clip over the gate's bar.
          match: Math.floor(checked.matchRate * 10_000) / 10_000,
          unheard: words.flatMap(({ unit }, i) => (checked.words[i]?.matched ? [] : [unit.display])).slice(0, 200),
        };
      } catch (err) {
        speech = { status: "unavailable", reason: `the recogniser failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 400) };
      }
    } else {
      speech = { status: "unavailable", reason: (inputs.asrUnavailableReason ?? "no recogniser configured").slice(0, 400) };
    }

    segments.push({
      id: scene.id,
      wav,
      narration: {
        audio: clipName(scene.id),
        audio_sha256: sha256(wav),
        duration_seconds: Math.round(duration * 1000) / 1000,
        // A mark after the last word takes that word's end, which the
        // aligner's minimum word length can push past the clip: clamp it.
        marks: Object.fromEntries(Object.entries(marks).map(([name, at]) => [name, Math.round(Math.min(at, duration) * 1000) / 1000])),
        words: words.map(({ unit }, i) => {
          const t = seeded.words[i] as { start: number; end: number; matched: boolean };
          const start = Math.round(Math.min(t.start, duration) * 1000) / 1000;
          const end = Math.round(Math.min(t.end, duration) * 1000) / 1000;
          return { text: unit.display, start, end: Math.max(start, end), matched: t.matched };
        }),
        speech,
      },
    });
  }
  if (!segments.length) throw new NarrationError("the script has no section with words to speak");

  const draft: NarrationTiming = {
    timing_version: TIMING_VERSION,
    composition: definition.id,
    script_sha256: sha256(inputs.script),
    voice: tts.describe,
    segments: Object.fromEntries(segments.map((s) => [s.id, s.narration])),
  };
  // Never hand back a table that compose would refuse.
  const checked = validateTiming(draft);
  if (!checked.ok) throw new NarrationError(`the timing table would be invalid:\n  ${checked.errors.join("\n  ")}`);
  return { timing: checked.timing, segments };
}
