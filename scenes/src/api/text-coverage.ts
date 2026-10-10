/**
 * Which characters the bundled fonts can draw. A trace string containing any
 * other character is refused at validation time, naming the character, so a
 * render never shows a missing-glyph box (tofu) or silently falls back to a
 * machine-dependent system font.
 */
import { GLYPH_RANGES } from "./glyph-coverage";

export function isCovered(codePoint: number): boolean {
  let lo = 0;
  let hi = GLYPH_RANGES.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const [first, last] = GLYPH_RANGES[mid] as readonly [number, number];
    if (codePoint < first) hi = mid - 1;
    else if (codePoint > last) lo = mid + 1;
    else return true;
  }
  return false;
}

/** Distinct characters in `text` the bundled fonts cannot draw, in order of appearance. */
export function uncoveredCharacters(text: string): string[] {
  const seen = new Set<string>();
  for (const ch of text) {
    const cp = ch.codePointAt(0) as number;
    if (!isCovered(cp)) seen.add(ch);
  }
  return [...seen];
}

/** "日" -> `"日" (U+65E5)` */
export function describeCharacter(ch: string): string {
  const cp = (ch.codePointAt(0) as number).toString(16).toUpperCase().padStart(4, "0");
  return `${JSON.stringify(ch)} (U+${cp})`;
}
