/**
 * The one component every adapter renders. It owns what a scene must not be
 * able to skip: the canvas, the layout, the timeline, and the plan-D2 mode
 * label — drawn here, after (above) the scene, from the trace's own metadata.
 * A scene therefore cannot omit or restyle the label.
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
      <Scene trace={trace} frame={frame} layout={layout} timeline={timeline} />
      <ModeLabel meta={trace.meta} layout={layout} />
    </div>
  );
}

/** Duration in frames of `scene` for `trace` at the studio frame rate. */
export function sceneDuration(scene: SceneDefinition, trace: SceneTrace): number {
  return scene.timeline(trace, STUDIO_FPS).durationInFrames;
}
