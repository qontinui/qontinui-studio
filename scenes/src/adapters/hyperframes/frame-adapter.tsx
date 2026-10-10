/**
 * The HyperFrames `FrameAdapter` for studio scenes (plan Phase 2, HyperFrames
 * adapter spike): `seekFrame(f)` renders `<SceneRoot frame={f}/>` with a
 * synchronous React commit (`flushSync`), so the DOM holds frame f before
 * `seekFrame` returns and HyperFrames captures it.
 *
 * Scenes are pure functions of `(trace, frame)` (src/api/scene.ts), which is
 * exactly HyperFrames' determinism contract for a frame adapter: any seek
 * order, the same state for the same frame, no wall clock, nothing pending
 * after the seek returns.
 *
 * `frames` maps the composition's own frame index to a scene frame. A full
 * render uses the identity (0..duration-1); `renderStills` renders a short
 * composition whose i-th frame is the i-th requested scene frame, because the
 * HyperFrames CLI has no single-frame option.
 */
import type { FrameAdapter, FrameAdapterContext } from "@hyperframes/core";
import { flushSync } from "react-dom";
import type { Root } from "react-dom/client";

import type { Aspect } from "../../api/layout";
import type { SceneDefinition } from "../../api/scene";
import { SceneRoot } from "../../api/scene-root";
import type { SceneTrace } from "../../api/trace/schema";

export interface StudioFrameAdapterOptions {
  id: string;
  root: Root;
  scene: SceneDefinition;
  trace: SceneTrace;
  aspect: Aspect;
  /** Scene frame for each composition frame. */
  frames: readonly number[];
}

export function createStudioFrameAdapter(options: StudioFrameAdapterOptions): FrameAdapter {
  const { root, scene, trace, aspect, frames } = options;
  if (frames.length === 0) throw new RangeError("a studio composition needs at least one frame");
  let destroyed = false;
  return {
    id: options.id,
    init: (_context: FrameAdapterContext) => undefined,
    getDurationFrames: () => frames.length,
    seekFrame: (frame: number) => {
      if (destroyed) throw new Error(`frame adapter ${options.id} was destroyed`);
      // HyperFrames seeks to floor(frame)/fps; clamp so the end of the
      // timeline maps to the last frame rather than past it.
      const index = Math.min(frames.length - 1, Math.max(0, Math.floor(frame)));
      const sceneFrame = frames[index] as number;
      // `key={sceneFrame}` mounts a fresh DOM tree for every frame. Without it
      // React updates the previous frame's DOM in place, and under
      // HyperFrames' software compositing (--disable-gpu-compositing) the
      // re-raster of the changed regions left antialiased edges that depended
      // on render history: 336 of 426 frames differed (up to 115 pixels, up to
      // 9 levels, on rounded corners and timeline markers) between a 6-worker
      // and a 1-worker render. With the remount, every frame equals the same
      // frame rendered alone (docs/hyperframes-spike.md).
      flushSync(() => {
        root.render(<SceneRoot key={sceneFrame} scene={scene} trace={trace} frame={sceneFrame} aspect={aspect} />);
      });
    },
    destroy: () => {
      destroyed = true;
      root.unmount();
    },
  };
}
