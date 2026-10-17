/**
 * Public types of `@qontinui/studio-scenes/player`, the embeddable package
 * built by `pnpm embed:pack` from `src/adapters/remotion/embed.ts`. The pack
 * ships this file as `dist/player.d.ts`.
 *
 * It is a `.ts` file of declarations only, so the studio's typecheck checks it
 * (`skipLibCheck` would skip a `.d.ts`). It is hand-written so the package
 * needs no zod or studio-internal types. `tests/unit/embed-types.test.ts`
 * holds it to the source: a change to the player's exports or props fails
 * typecheck until this file follows.
 */
import type { JSX } from "react";

/** The ids of the scenes this package ships (the studio catalogue). */
export type SceneId =
  | "s1-plan-lifecycle"
  | "s2-digital-twin"
  | "s3-gates-continuations"
  | "s4-merge-train"
  | "s5-fleet"
  | "s6-self-correction"
  | "s7-gui-automation";

export type Aspect = "16:9" | "9:16" | "1:1";

/**
 * Props of the web player.
 *
 * Reduced motion: under `prefers-reduced-motion: reduce` the player never
 * autoplays, whatever `autoPlay` says. It shows the scene's poster frame with
 * a play control (shown even when `controls` is false). Until the preference
 * is known (server rendering) it behaves as if motion were reduced.
 */
export interface PlayerEmbedProps {
  scene: string;
  /** Untrusted input: the player validates it and refuses an unproven replay. */
  trace: unknown;
  aspect: Aspect;
  /** Base URL the bundled fonts are served from (it contains `fonts/`). */
  assetBaseUrl: string;
  autoPlay?: boolean;
  loop?: boolean;
  controls?: boolean;
  className?: string;
  style?: Record<string, string | number>;
}

export declare function StudioPlayer(props: PlayerEmbedProps): JSX.Element;

/** `prefers-reduced-motion: reduce`, live; true while it cannot be known. */
export declare function usePrefersReducedMotion(): boolean;

/** Each scene's committed illustrative trace (untrusted JSON; mode "illustrative"). */
export declare const illustrativeTraces: Readonly<Record<string, unknown>>;

/** Scene id -> title. */
export declare const sceneTitles: Readonly<Record<string, string>>;
