import { describe, expect, it } from "vitest";

import { cubicBezier, easings, interpolate, mix, msToFrames, progress } from "../../src/api";

describe("interpolate", () => {
  it("maps linearly inside the range", () => {
    expect(interpolate(5, [0, 10], [0, 100])).toBe(50);
    expect(interpolate(15, [10, 20], [100, 0])).toBe(50);
  });

  it("clamps by default and extrapolates when asked", () => {
    expect(interpolate(-5, [0, 10], [0, 100])).toBe(0);
    expect(interpolate(20, [0, 10], [0, 100])).toBe(100);
    expect(interpolate(20, [0, 10], [0, 100], { clamp: false })).toBe(200);
  });

  it("handles multiple segments", () => {
    expect(interpolate(15, [0, 10, 20], [0, 100, 0])).toBe(50);
    expect(interpolate(10, [0, 10, 20], [0, 100, 0])).toBe(100);
  });

  it("applies the easing per segment", () => {
    const value = interpolate(5, [0, 10], [0, 100], { easing: (t) => t * t });
    expect(value).toBe(25);
  });

  it("rejects malformed ranges", () => {
    expect(() => interpolate(0, [0], [0])).toThrow(RangeError);
    expect(() => interpolate(0, [0, 1], [0])).toThrow(RangeError);
    expect(() => interpolate(0, [1, 1], [0, 1])).toThrow(RangeError);
  });
});

describe("easings", () => {
  it("every easing maps 0→0 and 1→1", () => {
    for (const [name, fn] of Object.entries(easings)) {
      expect(fn(0), name).toBeCloseTo(0, 6);
      expect(fn(1), name).toBeCloseTo(1, 6);
    }
  });

  it("cubicBezier matches the CSS 'ease' curve at known points", () => {
    const ease = cubicBezier(0.25, 0.1, 0.25, 1);
    // Reference values of cubic-bezier(.25,.1,.25,1) (CSS `ease`), from an
    // independent 100-step bisection of the same curve.
    expect(ease(0.5)).toBeCloseTo(0.80240339, 6);
    expect(ease(0.25)).toBeCloseTo(0.40851059, 6);
  });

  it("cubicBezier with linear control points is the identity", () => {
    const linear = cubicBezier(1 / 3, 1 / 3, 2 / 3, 2 / 3);
    for (const t of [0.1, 0.33, 0.5, 0.9]) expect(linear(t)).toBeCloseTo(t, 5);
  });

  it("cubicBezier is monotonic for the standard curve", () => {
    let prev = -1;
    for (let i = 0; i <= 100; i++) {
      const v = easings.standard(i / 100);
      expect(v).toBeGreaterThanOrEqual(prev);
      prev = v;
    }
  });

  it("cubicBezier rejects x control points outside [0, 1]", () => {
    expect(() => cubicBezier(-0.1, 0, 1, 1)).toThrow(RangeError);
  });

  it("is deterministic", () => {
    const a = Array.from({ length: 50 }, (_, i) => easings.enter(i / 49));
    const b = Array.from({ length: 50 }, (_, i) => easings.enter(i / 49));
    expect(a).toEqual(b);
  });
});

describe("frame helpers", () => {
  it("msToFrames rounds and refuses a bad fps", () => {
    expect(msToFrames(1000, 30)).toBe(30);
    expect(msToFrames(150, 30)).toBe(5);
    expect(msToFrames(-10, 30)).toBe(0);
    expect(() => msToFrames(100, 0)).toThrow(RangeError);
  });

  it("progress runs 0→1 over the window and steps for a zero duration", () => {
    expect(progress(9, 10, 10, easings.linear)).toBe(0);
    expect(progress(15, 10, 10, easings.linear)).toBe(0.5);
    expect(progress(25, 10, 10, easings.linear)).toBe(1);
    expect(progress(9, 10, 0)).toBe(0);
    expect(progress(10, 10, 0)).toBe(1);
  });

  it("mix", () => {
    expect(mix(10, 20, 0.25)).toBe(12.5);
  });
});
