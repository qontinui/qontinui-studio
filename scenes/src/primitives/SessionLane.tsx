import { attention, text, type AttentionLevel } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface SessionLaneProps {
  /** Top-left, in pixels. */
  x: number;
  y: number;
  width: number;
  label: string;
  /** Work segment on the lane, as fractions 0..1 of its width. */
  start: number;
  end: number;
  level: AttentionLevel;
  /** 0..1 entrance. */
  appear?: number;
  unit: number;
}

/** One agent session's lane: a track with the session's working segment. */
export function SessionLane({ x, y, width, label, start, end, level, appear = 1, unit }: SessionLaneProps) {
  const palette = attention[level];
  const trackHeight = Math.round(14 * unit);
  const s = Math.min(1, Math.max(0, start));
  const e = Math.min(1, Math.max(s, end));
  return (
    <div
      data-studio-lane={label}
      data-level={level}
      style={{ position: "absolute", left: x, top: y, width, opacity: appear, fontFamily: fontStack }}
    >
      <div
        style={{
          color: text.secondary,
          fontSize: Math.round(typeScale.caption * unit),
          fontWeight: fontWeights.semibold,
          marginBottom: Math.round(8 * unit),
        }}
      >
        {label}
      </div>
      <div
        style={{
          position: "relative",
          height: trackHeight,
          borderRadius: trackHeight,
          background: attention.inert.bg,
          border: `${Math.max(1, Math.round(1 * unit))}px solid ${attention.inert.border}`,
        }}
      >
        <div
          style={{
            position: "absolute",
            left: `${s * 100}%`,
            width: `${(e - s) * 100}%`,
            top: 0,
            bottom: 0,
            borderRadius: trackHeight,
            background: palette.border,
          }}
        />
      </div>
    </div>
  );
}
