/**
 * Web embed of a studio scene through `@remotion/player`. The same scene code
 * that renders the video renders here (plan D1). Consumers serve the
 * `assets/fonts/` directory and pass its parent as `assetBaseUrl`.
 *
 * Licence note (plan Phase 0): Remotion is source-available, not OSI. An
 * AGPL consumer (qontinui-web) embeds this only under its §7 additional
 * permission, loaded as a separate lazy chunk.
 */
import { Player } from "@remotion/player";

import type { PlayerEmbedProps } from "../../api/engine";
import { ASPECT_SIZES, STUDIO_FPS } from "../../api/layout";
import { sceneDuration } from "../../api/scene-root";
import { getScene } from "../../scenes";
import { StudioComposition, type StudioCompositionProps } from "./StudioComposition";

export function StudioPlayer({
  scene,
  trace,
  aspect,
  assetBaseUrl,
  autoPlay = false,
  loop = false,
  controls = true,
  className,
  style,
}: PlayerEmbedProps) {
  const { width, height } = ASPECT_SIZES[aspect];
  const inputProps: StudioCompositionProps = { sceneId: scene, trace, aspect, assetBaseUrl };
  return (
    <Player
      component={StudioComposition}
      inputProps={inputProps}
      durationInFrames={sceneDuration(getScene(scene), trace)}
      fps={STUDIO_FPS}
      compositionWidth={width}
      compositionHeight={height}
      autoPlay={autoPlay}
      loop={loop}
      controls={controls}
      className={className}
      style={{ width: "100%", aspectRatio: `${width} / ${height}`, ...style }}
    />
  );
}
