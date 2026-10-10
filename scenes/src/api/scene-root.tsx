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
