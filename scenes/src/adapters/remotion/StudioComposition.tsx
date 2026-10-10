/**
 * The Remotion composition component: maps Remotion's frame clock onto the
 * studio contract — `SceneRoot` with an explicit `frame` — so scenes never
 * call an engine hook themselves.
 */
import { useEffect, useState } from "react";
import { cancelRender, continueRender, delayRender, staticFile, useCurrentFrame } from "remotion";

import type { Aspect } from "../../api/layout";
import { SceneRoot } from "../../api/scene-root";
import type { SceneTrace } from "../../api/trace/schema";
import { getScene } from "../../scenes";
import { loadStudioFonts } from "../fonts";

export type StudioCompositionProps = {
  sceneId: string;
  trace: SceneTrace;
  aspect: Aspect;
  /**
   * Where `fonts/…` is served from. Unset in a CLI render (the bundle serves
   * `assets/` as its public dir); set by the web Player embed.
   */
  assetBaseUrl?: string;
};

function fontUrl(assetBaseUrl: string | undefined) {
  return (file: string) =>
    assetBaseUrl === undefined ? staticFile(file) : `${assetBaseUrl.replace(/\/$/, "")}/${file}`;
}

export function StudioComposition({ sceneId, trace, aspect, assetBaseUrl }: StudioCompositionProps) {
  const frame = useCurrentFrame();
  const [fontHandle] = useState(() => delayRender("Loading the studio fonts"));
  useEffect(() => {
    loadStudioFonts(assetBaseUrl ?? "remotion-static", fontUrl(assetBaseUrl))
      .then(() => continueRender(fontHandle))
      .catch((err: unknown) => cancelRender(err instanceof Error ? err : new Error(String(err))));
  }, [assetBaseUrl, fontHandle]);

  return <SceneRoot scene={getScene(sceneId)} trace={trace} frame={frame} aspect={aspect} />;
}
