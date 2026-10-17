/** Types for the vendored explainroo script.js (see ./README.md). */
export class ScriptError extends Error {
  line: number | undefined;
  constructor(message: string, line?: number);
}

export type ScriptUnit =
  | { type: "word"; display: string; spoken: string; space: boolean }
  | { type: "mark"; name: string }
  | { type: "pause"; sec: number | null; paragraph?: boolean };

export interface ScriptScene {
  id: string;
  attrs: Record<string, string | number>;
  line: number;
  units: ScriptUnit[];
  text: string;
  spoken: string;
}

export interface SpeechChunk {
  gapBefore: number;
  text: string;
  words: { unitIndex: number; display: string; spoken: string }[];
}

export function normWord(word: string): string;
export function tokenizeNarration(text: string, line?: number, language?: string): ScriptUnit[];
export function parseScript(source: string, options?: { language?: string }): { title: string | null; scenes: ScriptScene[] };
export function speechChunks(
  units: ScriptUnit[],
  options?: { sentenceGap?: number; paragraphGap?: number; pace?: number; language?: string },
): SpeechChunk[];
export function resolveMarks(
  units: ScriptUnit[],
  wordTimes: ReadonlyArray<{ start: number; end: number } | null | undefined>,
): Record<string, number>;
