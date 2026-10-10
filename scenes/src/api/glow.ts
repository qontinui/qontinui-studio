/**
 * Blur-free glow.
 *
 * Chrome caches blurred masks (box-shadow blur, filter: blur, text-shadow
 * blur) across paints in the same browser. A blurred glow therefore came out
 * differently depending on which frames had been rendered before it: frame 196
 * of S1 hashed one way alone and another way after frame 48 (9,179 px, up to
 * 4 levels, all inside the glow). Without the blur the frame was identical in
 * every order. So scenes and primitives never blur; lint enforces it. A glow is
 * drawn as solid, zero-blur rings of a translucent colour, which stack into a
 * stepped falloff.
 */

export interface Ring {
  /** Spread in pixels beyond the element's border box (>= 0). */
  spread: number;
  /** A token colour, usually translucent. */
  color: string;
}

/** CSS box-shadow value of solid rings (blur radius always 0), or "none". */
export function solidRings(rings: readonly Ring[]): string {
  const visible = rings.filter((r) => r.spread > 0);
  if (visible.length === 0) return "none";
  return visible.map((r) => `0 0 0 ${Math.round(r.spread)}px ${r.color}`).join(", ");
}

/**
 * A glow of `steps` rings out to `extent` pixels, scaled by `strength` (0..1).
 * Ring spreads are whole pixels, so partial coverage never depends on scale.
 */
export function glowRings(extent: number, strength: number, color: string, steps = 4): Ring[] {
  if (strength <= 0 || extent <= 0) return [];
  return Array.from({ length: steps }, (_, i) => ({
    spread: Math.round((extent * strength * (i + 1)) / steps),
    color,
  }));
}
