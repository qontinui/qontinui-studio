/** Types for the vendored explainroo align.js (see ./README.md). */
export interface AlignWord {
  spoken: string;
  display?: string;
}
export interface HeardWord {
  text: string;
  start: number;
  end: number;
}
export interface AlignedWord {
  start: number;
  end: number;
  matched: boolean;
}
export function intWords(n: number): string[];
export function yearWords(n: number): string[] | null;
export function expand(word: string, language?: string): string[];
export function similar(a: string, b: string): boolean;
export function alignWords(
  words: readonly AlignWord[],
  heard: readonly HeardWord[],
  duration: number,
  language?: string,
): { words: AlignedWord[]; matchRate: number };
