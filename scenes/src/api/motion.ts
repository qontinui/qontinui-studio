/**
 * Frame-driven motion: constants, easings and interpolation.
 *
 * Everything here is a pure function of numbers — no clock, no
 * requestAnimationFrame — so a scene built from it is a pure function of
 * `(trace, frame)` and two renders of one trace are frame-identical.
 *
 * The constants are studio-local for now. Plan D4 puts motion timings in
 * `@qontinui/design-tokens`; Phase 1 adds them there and this block becomes an
 * import. Values are chosen to match the platform-standard curves (Material 3
 * "standard"/"emphasized") so moving them later changes nothing visible.
 */

/** Durations in milliseconds. Convert with `msToFrames`. */
export const durations = {
  instant: 0,
  fast: 150,
  base: 300,
  slow: 600,
  deliberate: 900,
} as const;

export type EasingFn = (t: number) => number;

/**
 * Cubic Bézier easing, CSS `cubic-bezier(x1, y1, x2, y2)` semantics.
 * Solved by Newton iteration with a bisection fallback; deterministic.
 */
export function cubicBezier(x1: number, y1: number, x2: number, y2: number): EasingFn {
  if (x1 < 0 || x1 > 1 || x2 < 0 || x2 > 1) {
    throw new RangeError("cubicBezier: x1 and x2 must be within [0, 1]");
  }
  const cx = 3 * x1;
  const bx = 3 * (x2 - x1) - cx;
  const ax = 1 - cx - bx;
  const cy = 3 * y1;
  const by = 3 * (y2 - y1) - cy;
  const ay = 1 - cy - by;
  const sampleX = (t: number) => ((ax * t + bx) * t + cx) * t;
  const sampleY = (t: number) => ((ay * t + by) * t + cy) * t;
  const slopeX = (t: number) => (3 * ax * t + 2 * bx) * t + cx;

  const solveT = (x: number): number => {
    let t = x;
    for (let i = 0; i < 8; i++) {
      const err = sampleX(t) - x;
      if (Math.abs(err) < 1e-7) return t;
      const d = slopeX(t);
      if (Math.abs(d) < 1e-6) break;
      t -= err / d;
    }
    let lo = 0;
    let hi = 1;
    t = x;
    for (let i = 0; i < 40; i++) {
      const v = sampleX(t);
      if (Math.abs(v - x) < 1e-7) return t;
      if (v < x) lo = t;
      else hi = t;
      t = (lo + hi) / 2;
    }
    return t;
  };

  return (t: number) => {
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    return sampleY(solveT(t));
  };
}

export const easings = {
  linear: ((t: number) => t) as EasingFn,
  /** Material 3 standard. */
  standard: cubicBezier(0.2, 0, 0, 1),
  /** Material 3 emphasized decelerate — entrances. */
  enter: cubicBezier(0.05, 0.7, 0.1, 1),
  /** Material 3 emphasized accelerate — exits. */
  exit: cubicBezier(0.3, 0, 0.8, 0.15),
  inOutCubic: ((t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)) as EasingFn,
} as const;

export type EasingName = keyof typeof easings;

/** Milliseconds → whole frames at `fps` (rounded, never negative). */
export function msToFrames(ms: number, fps: number): number {
  if (!(fps > 0)) throw new RangeError("msToFrames: fps must be > 0");
  return Math.max(0, Math.round((ms / 1000) * fps));
}

export interface InterpolateOptions {
  easing?: EasingFn;
  /** Clamp outside the input range (default: true on both sides). */
  clamp?: boolean;
}

/**
 * Piecewise-linear map from `input` to `output` ranges (same length ≥ 2,
 * `input` strictly increasing), with an optional easing applied per segment.
 */
export function interpolate(
  value: number,
  input: readonly number[],
  output: readonly number[],
  options: InterpolateOptions = {},
): number {
  if (input.length < 2 || input.length !== output.length) {
    throw new RangeError("interpolate: input and output need the same length (>= 2)");
  }
  for (let i = 1; i < input.length; i++) {
    if (!((input[i] as number) > (input[i - 1] as number))) {
      throw new RangeError("interpolate: input range must be strictly increasing");
    }
  }
  const easing = options.easing ?? easings.linear;
  const clamp = options.clamp ?? true;
  const first = input[0] as number;
  const last = input[input.length - 1] as number;
  if (clamp) {
    if (value <= first) return output[0] as number;
    if (value >= last) return output[output.length - 1] as number;
  }
  let seg = 0;
  while (seg < input.length - 2 && value > (input[seg + 1] as number)) seg++;
  const i0 = input[seg] as number;
  const i1 = input[seg + 1] as number;
  const o0 = output[seg] as number;
  const o1 = output[seg + 1] as number;
  const t = (value - i0) / (i1 - i0);
  return o0 + (o1 - o0) * easing(t);
}

/**
 * Progress 0..1 of a transition that starts at `startFrame` and lasts
 * `durationFrames`, eased. A zero duration is a step at `startFrame`.
 */
export function progress(
  frame: number,
  startFrame: number,
  durationFrames: number,
  easing: EasingFn = easings.standard,
): number {
  if (durationFrames <= 0) return frame >= startFrame ? 1 : 0;
  return interpolate(frame, [startFrame, startFrame + durationFrames], [0, 1], { easing });
}

/** Linear mix of two numbers. */
export function mix(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}
