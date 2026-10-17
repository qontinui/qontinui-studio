/**
 * The embeddable package's hand-written public types (`embed/player-api.ts`)
 * must describe the module that is actually bundled
 * (`src/adapters/remotion/embed.ts`). These checks run in `pnpm typecheck`
 * (expectTypeOf) and in `pnpm test` (the scene-id list).
 */
import { describe, expect, expectTypeOf, it } from "vitest";

import type * as Public from "../../embed/player-api";
import * as Source from "../../src/adapters/remotion/embed";
import type { PlayerEmbedProps } from "../../src/api/engine";
import type { Aspect } from "../../src/api/layout";
import { scenes } from "../../src/scenes";

// Every scene id in the public type, and no other: `satisfies` refuses a
// missing key and an excess one.
const PUBLIC_SCENE_IDS = {
  "s1-plan-lifecycle": true,
  "s2-digital-twin": true,
  "s3-gates-continuations": true,
  "s4-merge-train": true,
  "s5-fleet": true,
  "s6-self-correction": true,
  "s7-gui-automation": true,
} as const satisfies Record<Public.SceneId, true>;

describe("embed/player-api.ts", () => {
  it("matches the bundled module's types", () => {
    expectTypeOf<Public.PlayerEmbedProps>().toEqualTypeOf<PlayerEmbedProps>();
    expectTypeOf<Public.Aspect>().toEqualTypeOf<Aspect>();
    expectTypeOf(Source.StudioPlayer).parameter(0).toEqualTypeOf<Public.PlayerEmbedProps>();
    expectTypeOf(Source.StudioPlayer).returns.toEqualTypeOf<ReturnType<typeof Public.StudioPlayer>>();
    expectTypeOf(Source.usePrefersReducedMotion).toEqualTypeOf<typeof Public.usePrefersReducedMotion>();
    expectTypeOf(Source.illustrativeTraces).toEqualTypeOf<typeof Public.illustrativeTraces>();
    expectTypeOf(Source.sceneTitles).toEqualTypeOf<typeof Public.sceneTitles>();
  });

  it("declares exactly the module's value exports", () => {
    type Exports = "StudioPlayer" | "usePrefersReducedMotion" | "illustrativeTraces" | "sceneTitles";
    expectTypeOf<keyof typeof Source>().toEqualTypeOf<Exports>();
    expectTypeOf<keyof typeof Public>().toEqualTypeOf<Exports>();
    expect(Object.keys(Source).sort()).toEqual(["StudioPlayer", "illustrativeTraces", "sceneTitles", "usePrefersReducedMotion"]);
  });

  it("names exactly the registered scenes", () => {
    expect(Object.keys(PUBLIC_SCENE_IDS).sort()).toEqual(Object.keys(scenes).sort());
  });

  it("ships an illustrative trace and a title for every scene", () => {
    expect(Object.keys(Source.illustrativeTraces).sort()).toEqual(Object.keys(scenes).sort());
    expect(Object.keys(Source.sceneTitles).sort()).toEqual(Object.keys(scenes).sort());
  });
});
