/**
 * The narration timing table (plan Phase 5): what a voice-over measured, kept
 * APART from scene code so re-voicing never edits a scene (the
 * scosman/videowright pattern). One table per composition and voice; it is
 * produced by the narration step (speak -> re-transcribe -> align) and read by
 * `resolveProgram`, which fits each segment's visuals to its narration.
 *
 * A table names, per composition segment:
 * - the segment's narration clip (a file beside the table, by name and
 *   SHA-256 — a clip that does not hash to the table's value is refused);
 * - the clip's length;
 * - its marks: `[#name]` cues from the script, resolved to seconds into the
 *   clip. A mark named after an event id of the segment's trace pins that
 *   event's beat to the moment it is spoken;
 * - optionally, the aligned words (captions are built from them) and the
 *   independent speech check (Whisper's re-transcription matched against the
 *   script), which the publish gate reads.
 *
 * Bump `TIMING_VERSION` on a breaking change.
 */
import { z } from "zod";

export const TIMING_VERSION = 1 as const;

/** Segment ids, mark names: the cue grammar's `[A-Za-z0-9_-]`. */
export const CUE_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

const cueName = z.string().min(1).max(64).regex(CUE_NAME, "names use [A-Za-z0-9_-]");
const seconds = z.number().finite().nonnegative();
const sha256 = z.string().regex(/^[0-9a-f]{64}$/, "a lower-case hex SHA-256");

export const timedWord = z
  .object({
    /** What the viewer reads (the `shown` side of `{shown|spoken}`). */
    text: z.string().min(1).max(200),
    start: seconds,
    end: seconds,
    /** Whether the timing source confirmed this word (false: interpolated). */
    matched: z.boolean(),
  })
  .strict();
export type TimedWord = z.infer<typeof timedWord>;

/**
 * The independent check: the clip re-transcribed by a speech recogniser and
 * aligned against the script. `match` is the share of script tokens heard
 * (0..1). `unavailable` means no recogniser answered; the gate treats that
 * as a failure, never as a pass.
 */
export const speechCheck = z.discriminatedUnion("status", [
  z
    .object({
      status: z.literal("measured"),
      /** e.g. "whisper-1 @ http://host:port" — what heard the clip. */
      by: z.string().min(1).max(200),
      match: z.number().min(0).max(1),
      /** Script words the recogniser did not hear (for the review page). */
      unheard: z.array(z.string().max(200)).max(200).default([]),
    })
    .strict(),
  z
    .object({
      status: z.literal("unavailable"),
      reason: z.string().min(1).max(400),
    })
    .strict(),
]);
export type SpeechCheck = z.infer<typeof speechCheck>;

export const segmentNarration = z
  .object({
    /** The clip's file name, in the table's own directory (no path). */
    audio: z.string().min(1).max(128).regex(/^[A-Za-z0-9._-]+$/, "a plain file name"),
    audio_sha256: sha256,
    duration_seconds: z.number().finite().positive().max(600),
    /** `[#name]` cues, seconds into the clip. */
    marks: z.record(cueName, seconds).default({}),
    words: z.array(timedWord).max(5000).default([]),
    speech: speechCheck.optional(),
  })
  .strict()
  .superRefine((segment, ctx) => {
    for (const [name, at] of Object.entries(segment.marks)) {
      if (at > segment.duration_seconds + 1e-6) {
        ctx.addIssue({ code: "custom", message: `mark "${name}" at ${at}s is past the clip's end (${segment.duration_seconds}s)` });
      }
    }
    let previous = 0;
    for (const [i, word] of segment.words.entries()) {
      if (word.end < word.start) ctx.addIssue({ code: "custom", message: `words[${i}] ends before it starts` });
      if (word.end > segment.duration_seconds + 1e-6) {
        ctx.addIssue({ code: "custom", message: `words[${i}] ends at ${word.end}s, past the clip's end (${segment.duration_seconds}s)` });
      }
      if (word.start + 1e-6 < previous) ctx.addIssue({ code: "custom", message: `words[${i}] starts before the word before it` });
      previous = word.start;
    }
  });
export type SegmentNarration = z.infer<typeof segmentNarration>;

export const narrationTiming = z
  .object({
    timing_version: z.literal(TIMING_VERSION),
    /** The composition this was voiced for. */
    composition: cueName,
    /** SHA-256 of the script the clips were voiced from. */
    script_sha256: sha256,
    /** The voice that spoke it, e.g. "kokoro af_heart @ http://host:8880". */
    voice: z.string().min(1).max(200),
    segments: z.record(cueName, segmentNarration),
  })
  .strict();
export type NarrationTiming = z.infer<typeof narrationTiming>;

export type TimingValidation = { ok: true; timing: NarrationTiming } | { ok: false; errors: string[] };

export function validateTiming(input: unknown): TimingValidation {
  const result = narrationTiming.safeParse(input);
  if (result.success) return { ok: true, timing: result.data };
  return {
    ok: false,
    errors: result.error.issues.map((issue) => `${issue.path.length ? issue.path.join(".") : "(root)"}: ${issue.message}`),
  };
}
