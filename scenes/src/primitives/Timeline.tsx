import { attention, border, text, type AttentionLevel } from "../api/tokens";
import { fontStack, typeScale } from "../api/typography";

export interface TimelineTick {
  /** Position along the axis, 0..1. */
  at: number;
  level: AttentionLevel;
  /** 0..1 how far this tick has appeared. */
  appear: number;
}

export interface TimelineProps {
  /** Left end of the axis, in pixels. */
  x: number;
  y: number;
  width: number;
  ticks: readonly TimelineTick[];
  /** Playhead position 0..1. */
  playhead: number;
  startLabel: string;
  endLabel: string;
  unit: number;
}

/** A horizontal time axis with event ticks and a playhead. */
export function Timeline({ x, y, width, ticks, playhead, startLabel, endLabel, unit }: TimelineProps) {
  const axis = Math.max(2, Math.round(3 * unit));
  const tickSize = Math.round(16 * unit);
  const p = Math.min(1, Math.max(0, playhead));
  const labelStyle = {
    position: "absolute" as const,
    top: Math.round(18 * unit),
    color: text.muted,
    fontFamily: fontStack,
    fontSize: Math.round(typeScale.caption * 0.9 * unit),
    fontVariantNumeric: "tabular-nums" as const,
    whiteSpace: "nowrap" as const,
  };
  return (
    <div data-studio-timeline style={{ position: "absolute", left: x, top: y, width, height: 1 }}>
      <div style={{ position: "absolute", left: 0, top: -axis / 2, width, height: axis, background: border.default, borderRadius: axis }} />
      <div style={{ position: "absolute", left: 0, top: -axis / 2, width: width * p, height: axis, background: text.secondary, borderRadius: axis }} />
      {ticks.map((tick, i) => {
        const palette = attention[tick.level];
        const size = tickSize * (0.4 + 0.6 * tick.appear);
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: tick.at * width - size / 2,
              top: -size / 2,
              width: size,
              height: size,
              borderRadius: size,
              background: palette.bg,
              border: `${Math.max(1, Math.round(2 * unit))}px solid ${palette.border}`,
              boxSizing: "border-box",
              opacity: tick.appear,
            }}
          />
        );
      })}
      <div
        style={{
          position: "absolute",
          left: p * width - Math.round(2 * unit),
          top: -Math.round(18 * unit),
          width: Math.round(4 * unit),
          height: Math.round(36 * unit),
          background: text.primary,
          borderRadius: Math.round(2 * unit),
        }}
      />
      <div style={{ ...labelStyle, left: 0 }}>{startLabel}</div>
      <div style={{ ...labelStyle, right: 0 }}>{endLabel}</div>
    </div>
  );
}
