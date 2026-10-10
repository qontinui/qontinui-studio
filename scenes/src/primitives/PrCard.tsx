import { attention, border, surface, text, type AttentionLevel } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface PrCardProps {
  /** Top-left, in pixels. */
  x: number;
  y: number;
  width: number;
  repo: string;
  number: number;
  title: string;
  /** Status badge text, e.g. "CI running". */
  status: string;
  /** Badge colour — an attention level. */
  level: AttentionLevel;
  /** 0..1 entrance. */
  appear?: number;
  unit: number;
}

/** A pull request: repo#number, title and a status badge. */
export function PrCard({ x, y, width, repo, number, title, status, level, appear = 1, unit }: PrCardProps) {
  const palette = attention[level];
  const accent = "accent" in palette ? palette.accent : palette.border;
  return (
    <div
      data-studio-pr={`${repo}#${number}`}
      data-level={level}
      style={{
        position: "absolute",
        left: x,
        top: y,
        width,
        boxSizing: "border-box",
        padding: `${Math.round(16 * unit)}px ${Math.round(20 * unit)}px`,
        background: surface.raised,
        border: `${Math.max(1, Math.round(1.5 * unit))}px solid ${border.default}`,
        borderLeft: `${Math.round(6 * unit)}px solid ${accent}`,
        borderRadius: Math.round(12 * unit),
        opacity: appear,
        transform: `translateY(${(1 - appear) * 20 * unit}px)`,
        fontFamily: fontStack,
        display: "flex",
        flexDirection: "column",
        gap: Math.round(8 * unit),
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: Math.round(12 * unit),
        }}
      >
        <span
          style={{
            color: text.muted,
            fontSize: Math.round(typeScale.caption * unit),
            fontVariantNumeric: "tabular-nums",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
            minWidth: 0,
          }}
        >
          {repo}#{number}
        </span>
        <span
          data-studio-pr-status={status}
          style={{
            padding: `${Math.round(3 * unit)}px ${Math.round(10 * unit)}px`,
            borderRadius: Math.round(999 * unit),
            background: palette.bg,
            color: palette.fg,
            border: `${Math.max(1, Math.round(1.5 * unit))}px solid ${palette.border}`,
            fontSize: Math.round(typeScale.caption * 0.9 * unit),
            fontWeight: fontWeights.semibold,
            whiteSpace: "nowrap",
          }}
        >
          {status}
        </span>
      </div>
      <div
        style={{
          color: text.primary,
          fontSize: Math.round(typeScale.body * unit),
          fontWeight: fontWeights.semibold,
          lineHeight: 1.25,
        }}
      >
        {title}
      </div>
    </div>
  );
}
