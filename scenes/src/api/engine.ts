/**
 * The engine seam (plan D7). Scenes and primitives never see an engine; an
 * adapter turns `(scene, trace)` into frames. Remotion is the first adapter
 * (`src/adapters/remotion/`); a second engine is another implementation of
 * these interfaces, with no scene changes.
 */
import type { Aspect } from "./layout";
import type { SceneTrace } from "./trace/schema";

export const OUTPUT_FORMATS = ["mp4", "webm", "gif", "png"] as const;
export type OutputFormat = (typeof OUTPUT_FORMATS)[number];

export interface RenderRequest {
  /** Scene id from the registry. */
  scene: string;
  trace: SceneTrace;
  format: OutputFormat;
  aspect: Aspect;
  /**
   * Output path. For mp4/webm/gif and for a png still (`frame` set) this is a
   * file; for a png frame sequence (`frame` unset) it is a directory.
   */
  outPath: string;
  /** png only: render just this frame as a still. */
  frame?: number;
  /** Output scale factor (1 = native 1920x1080 / 1080x1920 / 1080x1080). */
  scale?: number;
  /**
   * Replace an existing output file, or a previous `element-*.png` sequence.
   * Without it an existing target is refused. Nothing else is ever deleted.
   */
  overwrite?: boolean;
  onProgress?: (fraction: number) => void;
}

export interface RenderResult {
  /** Files written, in frame order for a sequence. */
  files: string[];
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
}

export interface StillsRequest {
  scene: string;
  trace: SceneTrace;
  aspect: Aspect;
  frames: readonly number[];
  /** Directory; files are named `<prefix>f<frame:4>.png`. */
  outDir: string;
  prefix?: string;
  scale?: number;
  /** Replace existing files of the same names (default: refuse). */
  overwrite?: boolean;
}

export interface EngineAdapter {
  readonly name: string;
  render(request: RenderRequest): Promise<RenderResult>;
  /** Render chosen frames to PNG (golden tests, posters). */
  renderStills(request: StillsRequest): Promise<string[]>;
  /** Release long-lived resources (browser, bundle). */
  close(): Promise<void>;
}

/** Props of an adapter's embeddable web player. */
export interface PlayerEmbedProps {
  scene: string;
  /** Untrusted input: the player validates it and refuses an unproven replay. */
  trace: unknown;
  aspect: Aspect;
  /** Base URL the bundled fonts are served from (contains `fonts/`). */
  assetBaseUrl: string;
  autoPlay?: boolean;
  loop?: boolean;
  controls?: boolean;
  className?: string;
  style?: Record<string, string | number>;
}
