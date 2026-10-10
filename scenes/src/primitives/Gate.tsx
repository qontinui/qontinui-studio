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
  const leafWidth = size * 0.32;
  const postWidth = Math.max(3, size * 0.06);
  const span = leafWidth * 2 + postWidth * 2;
  // Opening folds each leaf against its post.
  const fold = 1 - 0.85 * o;
  const leafStyle = {
    position: "absolute" as const,
    top: size * 0.12,
    width: leafWidth,
    height: size * 0.76,
    background: palette.bg,
    border: `${Math.max(1, Math.round(2 * unit))}px solid ${palette.border}`,
    boxSizing: "border-box" as const,
    borderRadius: Math.round(4 * unit),
  };
  return (
    <div
      data-studio-gate={label}
      data-level={level}
      data-open={o}
      style={{
        position: "absolute",
        left: x - span / 2,
        top: y - size / 2,
        width: span,
        height: size,
        opacity: appear,
      }}
    >
      <div style={{ position: "absolute", left: 0, top: 0, width: postWidth, height: size, background: palette.fg, borderRadius: postWidth }} />
      <div style={{ position: "absolute", right: 0, top: 0, width: postWidth, height: size, background: palette.fg, borderRadius: postWidth }} />
      <div style={{ ...leafStyle, left: postWidth, transform: `scaleX(${fold})`, transformOrigin: "left center" }} />
      <div style={{ ...leafStyle, right: postWidth, transform: `scaleX(${fold})`, transformOrigin: "right center" }} />
      <div
        style={{
          position: "absolute",
          top: size + Math.round(10 * unit),
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
