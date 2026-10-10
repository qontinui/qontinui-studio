import { attention, type AttentionLevel } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface GateProps {
  /** Centre, in pixels. */
  x: number;
  y: number;
  /** Height of the gate posts. */
  size: number;
  label: string;
  /** 0 = closed, 1 = fully open. */
  open: number;
  level: AttentionLevel;
  appear?: number;
  unit: number;
}

/**
 * A gate: two leaves that part as `open` goes 0 → 1. Used for a condition
 * the work waits on (CI, a merge-train slot, a coord gate).
 */
export function Gate({ x, y, size, label, open, level, appear = 1, unit }: GateProps) {
  const palette = attention[level];
  const o = Math.min(1, Math.max(0, open));
  // All geometry is on whole pixels. With fractional edges (and a scaleX()
  // fold) one edge pixel of a leaf rasterised nondeterministically, ±1 in
  // one channel across renders of the same frame; the determinism test
  // caught it. Integer edges have no partial coverage to disagree about.
  const box = Math.round(size);
  const leafWidth = Math.round(box * 0.32);
  const postWidth = Math.max(3, Math.round(box * 0.06));
  const span = leafWidth * 2 + postWidth * 2;
  // Opening folds each leaf against its post by shrinking its width.
  const foldedWidth = Math.max(1, Math.round(leafWidth * (1 - 0.85 * o)));
  const leafStyle = {
    position: "absolute" as const,
    top: Math.round(box * 0.12),
    width: leafWidth,
    height: Math.round(box * 0.76),
    background: palette.bg,
    border: `${Math.max(1, Math.round(2 * unit))}px solid ${palette.border}`,
    boxSizing: "border-box" as const,
    borderRadius: Math.round(4 * unit),
  };
  return (
    <div
      data-studio-gate={label}
      data-level={level}
      style={{
        position: "absolute",
        left: Math.round(x - span / 2),
        top: Math.round(y - box / 2),
        width: span,
        height: box,
        opacity: appear,
      }}
    >
      <div style={{ position: "absolute", left: 0, top: 0, width: postWidth, height: box, background: palette.fg, borderRadius: postWidth }} />
      <div style={{ position: "absolute", right: 0, top: 0, width: postWidth, height: box, background: palette.fg, borderRadius: postWidth }} />
      <div style={{ ...leafStyle, left: postWidth, width: foldedWidth }} />
      <div style={{ ...leafStyle, right: postWidth, width: foldedWidth }} />
      <div
        style={{
          position: "absolute",
          top: box + Math.round(10 * unit),
          left: "50%",
          transform: "translateX(-50%)",
          whiteSpace: "nowrap",
          color: palette.fg,
          fontFamily: fontStack,
          fontSize: Math.round(typeScale.caption * unit),
          fontWeight: fontWeights.semibold,
        }}
      >
        {label}
      </div>
    </div>
  );
}
