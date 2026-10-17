/**
 * The one component every adapter renders. It owns what a scene must not be
 * able to skip: the canvas, the layout, the timeline, and the plan-D2 mode
 * label — drawn here, in a layer above the scene's isolated stacking context,
 * from the trace's own metadata. A scene therefore cannot omit, restyle or
 * cover the label.
 */
import { useMemo } from "react";

import { ModeLabel } from "../primitives/ModeLabel";
import { STUDIO_FPS, layoutFor, type Aspect } from "./layout";
import type { SceneDefinition } from "./scene";
import { surface } from "./tokens";
import type { SceneTrace } from "./trace/schema";
import { fontStack } from "./typography";

export interface SceneRootProps {
  scene: SceneDefinition;
  trace: SceneTrace;
  frame: number;
  aspect: Aspect;
}

export function SceneRoot({ scene, trace, frame, aspect }: SceneRootProps) {
  const layout = useMemo(() => layoutFor(aspect), [aspect]);
  const timeline = useMemo(() => scene.timeline(trace, STUDIO_FPS), [scene, trace]);
  const Scene = scene.Component;
  return (
    <div
      data-studio-scene={scene.id}
      data-studio-aspect={aspect}
      data-studio-frame={frame}
      style={{
        position: "relative",
        width: layout.width,
        height: layout.height,
        overflow: "hidden",
        background: surface.canvas,
        fontFamily: fontStack,
      }}
    >
      {/* The scene gets its own stacking context, so no z-index inside it can
          rise above the mode label. */}
      <div style={{ position: "absolute", inset: 0, isolation: "isolate", zIndex: 0 }}>
        <Scene trace={trace} frame={frame} layout={layout} timeline={timeline} />
      </div>
      <div style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none" }}>
        <ModeLabel meta={trace.meta} layout={layout} />
      </div>
    </div>
  );
}

/** Duration in frames of `scene` for `trace` at the studio frame rate. */
export function sceneDuration(scene: SceneDefinition, trace: SceneTrace): number {
  return scene.timeline(trace, STUDIO_FPS).durationInFrames;
}

/**
 * The poster frame of `scene` for `trace` (see `SceneDefinition.poster`):
 * the scene's own choice, or its last frame. A choice that is not a whole
 * frame inside the scene's duration is a scene bug, and throws.
 */
export function posterFrame(scene: SceneDefinition, trace: SceneTrace): number {
  const timeline = scene.timeline(trace, STUDIO_FPS);
  const last = timeline.durationInFrames - 1;
  const chosen = scene.poster ? scene.poster(timeline) : last;
  if (!Number.isInteger(chosen) || chosen < 0 || chosen > last) {
    throw new Error(`scene "${scene.id}": poster frame ${chosen} is not a whole frame in 0..${last}`);
  }
  return chosen;
}
