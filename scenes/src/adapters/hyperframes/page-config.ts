/** The render config a studio HyperFrames page reads (shared by Node and the page). */
import type { Aspect } from "../../api/layout";

export const STUDIO_CONFIG_ELEMENT_ID = "studio-config";

export interface StudioPageConfig {
  compositionId: string;
  mountId: string;
  sceneId: string;
  aspect: Aspect;
  width: number;
  height: number;
  fps: number;
  /** Scene frame for each composition frame. */
  frames: number[];
  /** The validated trace, as JSON (the page re-parses it). */
  trace: unknown;
}
