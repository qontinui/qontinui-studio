/**
 * Design tokens used by scenes and primitives — VENDORED, not imported.
 *
 * Authority: `qontinui-design-tokens/src/colors.ts` at
 * qontinui-design-tokens@82873452 (package.json version 1.2.0). Every value
 * below is copied verbatim from that file; the source lines are cited on each
 * group.
 *
 * Why vendored: `@qontinui/design-tokens` is licensed AGPL-3.0-or-later in its
 * repository (1.2.0), and this package is MIT (plan D7 / Phase 0: the studio
 * is MIT so Remotion can be its first adapter). The last npm release (1.0.0,
 * MIT) predates the attention palette entirely, so it cannot serve the state
 * colours this package needs either. When the token package ships a release
 * this package may consume (Phase 1 adds the motion and typography tokens
 * there), replace this file with an import and delete it.
 *
 * Rule: scenes mint no colour. State colours (PR card, gate, session lane)
 * come ONLY from `attention` below.
 */

/** colors.ts:12-19 `brand` */
export const brand = {
  primary: "#4A90D9",
  secondary: "#8B6BB5",
  success: "#4DB89D",
} as const;

/** colors.ts:25-34 `surface` */
export const surface = {
  canvas: "#111115",
  raised: "#1E1E22",
  hover: "#252529",
  active: "#2C2C32",
} as const;

/** colors.ts:40-49 `border` */
export const border = {
  subtle: "#2A2A30",
  default: "#3A3A42",
  strong: "#4A4A54",
  interactive: "#5A5A66",
} as const;

/** colors.ts:55-64 `text` */
export const text = {
  primary: "#FFFFFF",
  secondary: "#B4B4B4",
  muted: "#8A8A8A",
  inverse: "#111115",
} as const;

/** colors.ts:196-200 `glow` */
export const glow = {
  primary: "rgba(74, 144, 217, 0.2)",
  secondary: "rgba(139, 107, 181, 0.2)",
  success: "rgba(77, 184, 157, 0.2)",
} as const;

/**
 * colors.ts:107-169 `attentionPalette` — colour encodes WHO MUST ACT:
 * attention (red) someone must act now; waiting (amber) waiting on something
 * else; running (yellow) work in flight; testing (purple) rebasing /
 * re-running / verifying; landing (blue) on its way to done; done (green)
 * finished; inert (muted) nothing happening, nothing wrong.
 *
 * `accent` exists upstream only for attention and waiting; the other levels
 * have none, so this file does not invent one — use `border`.
 */
export const attention = {
  attention: {
    bg: "oklch(63.7% 0.237 25.331 / 0.15)",
    fg: "oklch(88.5% 0.062 18.334)",
    border: "oklch(63.7% 0.237 25.331 / 0.35)",
    accent: "oklch(63.7% 0.237 25.331 / 0.8)",
  },
  waiting: {
    bg: "oklch(76.9% 0.188 70.08 / 0.15)",
    fg: "oklch(92.4% 0.12 95.746)",
    border: "oklch(76.9% 0.188 70.08 / 0.3)",
    accent: "oklch(76.9% 0.188 70.08 / 0.8)",
  },
  running: {
    bg: "oklch(79.5% 0.184 86.047 / 0.15)",
    fg: "oklch(94.5% 0.129 101.54)",
    border: "oklch(79.5% 0.184 86.047 / 0.3)",
  },
  testing: {
    bg: "oklch(62.7% 0.265 303.9 / 0.15)",
    fg: "oklch(90.2% 0.063 306.703)",
    border: "oklch(62.7% 0.265 303.9 / 0.3)",
  },
  landing: {
    bg: "oklch(62.3% 0.214 259.815 / 0.15)",
    fg: "oklch(88.2% 0.059 254.128)",
    border: "oklch(62.3% 0.214 259.815 / 0.3)",
  },
  done: {
    bg: "oklch(72.3% 0.219 149.579 / 0.15)",
    fg: "oklch(92.5% 0.084 155.995)",
    border: "oklch(72.3% 0.219 149.579 / 0.3)",
  },
  /** Upstream these reference surface.active / text.muted / border.subtle. */
  inert: {
    bg: surface.active,
    fg: text.muted,
    border: border.subtle,
  },
} as const;

export type AttentionLevel = keyof typeof attention;

/** colors.ts:182-190 `attentionLevels` — most urgent first. */
export const attentionLevels: readonly AttentionLevel[] = [
  "attention",
  "waiting",
  "running",
  "testing",
  "landing",
  "done",
  "inert",
] as const;

export const tokens = { brand, surface, border, text, glow, attention } as const;
