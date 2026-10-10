import { attention, type AttentionLevel } from "../api/tokens";

export interface Point {
  x: number;
  y: number;
}

export interface EdgeProps {
  from: Point;
  to: Point;
  /** 0..1 how much of the edge is drawn in the state colour. */
  progress: number;
  level: AttentionLevel;
  unit: number;
  /** The overlay's size (the full frame). */
  frameWidth: number;
  frameHeight: number;
}

/**
 * A connection between two nodes: an inert track with the state colour drawn
 * over it from `from` towards `to` as `progress` goes 0 → 1.
 */
export function Edge({ from, to, progress, level, unit, frameWidth, frameHeight }: EdgeProps) {
  const p = Math.min(1, Math.max(0, progress));
  const head = { x: from.x + (to.x - from.x) * p, y: from.y + (to.y - from.y) * p };
  const strokeWidth = Math.max(2, 4 * unit);
  return (
    <svg
      width={frameWidth}
      height={frameHeight}
      viewBox={`0 0 ${frameWidth} ${frameHeight}`}
      style={{ position: "absolute", left: 0, top: 0, overflow: "visible" }}
    >
      <line
        x1={from.x}
        y1={from.y}
        x2={to.x}
        y2={to.y}
        stroke={attention.inert.border}
        strokeWidth={strokeWidth}
        strokeLinecap="round"
      />
      {p > 0 ? (
        <line
          x1={from.x}
          y1={from.y}
          x2={head.x}
          y2={head.y}
          stroke={attention[level].fg}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
        />
      ) : null}
    </svg>
  );
}
