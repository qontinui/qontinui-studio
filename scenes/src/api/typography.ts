/**
 * Typography for scenes. Studio-local until Phase 1 moves a typography scale
 * into `@qontinui/design-tokens` (plan D4); then this becomes an import.
 *
 * The family is the bundled Inter 4.1, full character set (SIL OFL 1.1,
 * `assets/fonts/`, from `inter-ui@4.1.1`), loaded by
 * the engine adapter before the first frame. No web font is fetched at render
 * time, so text rasterises identically on every run.
 */

/** The @font-face family name the adapters register. */
export const FONT_FAMILY = "Studio Inter";

/** CSS font-family stack. The generic fallback is never expected to be hit. */
export const fontStack = `"${FONT_FAMILY}", sans-serif`;

/** Font files bundled under `assets/fonts/`, keyed by weight. */
export const fontFiles = [
  { weight: 400, file: "fonts/Inter-Regular.woff2" },
  { weight: 600, file: "fonts/Inter-SemiBold.woff2" },
  { weight: 700, file: "fonts/Inter-Bold.woff2" },
] as const;

/**
 * Type scale in reference pixels at a 1080-pixel short edge; multiply by
 * `Layout.unit` for the actual size.
 */
export const typeScale = {
  caption: 22,
  body: 28,
  label: 24,
  title: 44,
  display: 64,
  counter: 56,
} as const;

export const fontWeights = {
  regular: 400,
  semibold: 600,
  bold: 700,
} as const;
