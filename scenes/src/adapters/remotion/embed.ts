/**
 * Entry point of the embeddable package (`pnpm embed:pack`, see `embed/`):
 * what a website imports to play a scene. It is bundled with esbuild into one
 * ESM module that leaves React and Remotion external, so the consumer supplies
 * them (peer dependencies) and can load this module as its own lazy chunk.
 *
 * The public types are `embed/player-api.ts` (shipped as `dist/player.d.ts`); `tests/unit/embed-types.test.ts`
 * holds them to this module.
 */
import { scenes } from "../../scenes";
import { illustrativeTraces } from "../../scenes/illustrative-traces";

export { StudioPlayer, usePrefersReducedMotion } from "./Player";
export { illustrativeTraces };

/** Scene id -> title, in catalogue order. */
export const sceneTitles: Readonly<Record<string, string>> = Object.fromEntries(
  Object.values(scenes).map((scene) => [scene.id, scene.title]),
);
