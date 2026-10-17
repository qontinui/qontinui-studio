/**
 * Captions for a narrated program (plan Phase 5: "captions present"). Built
 * from the timing table's aligned words, placed where each segment's clip
 * plays in the program, and written as WebVTT beside the video — the sidecar
 * every social network and the site's <track> take. Pure: the same program
 * and table always give the same file.
 */
import type { Program } from "./composition";
import type { NarrationTiming } from "./narration-timing";

export interface CaptionCue {
  start: number;
  end: number;
  text: string;
  segmentId: string;
}

/** A cue never grows past this many characters by joining words (a single longer word is its own cue). */
export const CAPTION_LINE_MAX = 42;
/** A cue lasts at least this long (a zero-length word still shows). */
const MIN_CUE_S = 0.1;
/** A silence this long between two words ends a cue. */
const CUE_GAP_S = 0.6;

const SENTENCE_END = /[.!?…]["'’)\]]*$/;

export function captionCues(program: Program, timing: NarrationTiming): CaptionCue[] {
  const cues: CaptionCue[] = [];
  for (const clip of program.audio) {
    const narration = timing.segments[clip.segmentId];
    if (!narration) continue;
    const offset = clip.startFrame / program.fps;
    const clipEnd = (clip.startFrame + clip.durationInFrames) / program.fps;
    let current: { start: number; end: number; words: string[] } | null = null;
    const close = () => {
      if (current && current.words.length) {
        cues.push({ start: current.start, end: Math.min(Math.max(current.end, current.start + MIN_CUE_S), clipEnd), text: current.words.join(" "), segmentId: clip.segmentId });
      }
      current = null;
    };
    for (const word of narration.words) {
      const start = offset + word.start;
      const end = offset + word.end;
      if (current) {
        const c = current as { start: number; end: number; words: string[] };
        const longer = [...c.words, word.text].join(" ").length > CAPTION_LINE_MAX;
        if (longer || start - c.end > CUE_GAP_S) close();
      }
      if (!current) current = { start, end, words: [] };
      const c = current as { start: number; end: number; words: string[] };
      c.words.push(word.text);
      c.end = Math.max(c.end, end);
      if (SENTENCE_END.test(word.text)) close();
    }
    close();
  }
  // A cue ends no later than the next one starts.
  for (let i = 0; i + 1 < cues.length; i++) {
    const cue = cues[i] as CaptionCue;
    const next = cues[i + 1] as CaptionCue;
    if (cue.end > next.start) cue.end = next.start;
  }
  return cues.filter((cue) => cue.end > cue.start);
}

function timestamp(seconds: number): string {
  const ms = Math.round(seconds * 1000);
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  const rest = ms % 1000;
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}.${pad(rest, 3)}`;
}

/** WebVTT text for `cues`. Cue text is escaped (`&`, `<`, `>`). */
export function toWebVtt(cues: readonly CaptionCue[]): string {
  const escape = (text: string) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const body = cues.map((cue, i) => `${i + 1}\n${timestamp(cue.start)} --> ${timestamp(cue.end)}\n${escape(cue.text)}\n`);
  return `WEBVTT\n\n${body.join("\n")}`;
}
