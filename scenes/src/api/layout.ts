/** Output aspects and the layout box a scene draws into. */

export const ASPECTS = ["16:9", "9:16", "1:1"] as const;
export type Aspect = (typeof ASPECTS)[number];

export const ASPECT_SIZES: Record<Aspect, { width: number; height: number }> = {
  "16:9": { width: 1920, height: 1080 },
  "9:16": { width: 1080, height: 1920 },
  "1:1": { width: 1080, height: 1080 },
};

/** Fixed frame rate for every studio render (determinism: no variable fps). */
export const STUDIO_FPS = 30;

export type Orientation = "landscape" | "portrait" | "square";

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Layout {
  aspect: Aspect;
  width: number;
  height: number;
  orientation: Orientation;
  /** Scale factor: short edge / 1080. Multiply reference sizes by this. */
  unit: number;
  /** Content area inside the title-safe margin. */
  safe: Rect;
}

export function isAspect(value: string): value is Aspect {
  return (ASPECTS as readonly string[]).includes(value);
}

/** Composition-id-safe spelling of an aspect, e.g. "16x9". */
export function aspectSlug(aspect: Aspect): string {
  return aspect.replace(":", "x");
}

export function layoutFor(aspect: Aspect): Layout {
  const { width, height } = ASPECT_SIZES[aspect];
  const orientation: Orientation =
    width > height ? "landscape" : width < height ? "portrait" : "square";
  const unit = Math.min(width, height) / 1080;
  const margin = Math.round(72 * unit);
  return {
    aspect,
    width,
    height,
    orientation,
    unit,
    safe: { x: margin, y: margin, width: width - 2 * margin, height: height - 2 * margin },
  };
}

/** A scale must give whole, even pixel dimensions at every aspect (H.264/VP8 need even sizes). */
export function scaleProblem(scale: number): string | null {
  if (!(scale > 0 && scale <= 4)) return "--scale must be in (0, 4]";
  for (const aspect of ASPECTS) {
    const { width, height } = ASPECT_SIZES[aspect];
    for (const size of [width * scale, height * scale]) {
      const rounded = Math.round(size);
      if (Math.abs(size - rounded) > 1e-9 || rounded % 2 !== 0) {
        return `--scale ${scale} gives ${width * scale}x${height * scale} at ${aspect}; it must give even whole pixels at every aspect (e.g. 0.25, 0.5, 1, 2)`;
      }
    }
  }
  return null;
}
