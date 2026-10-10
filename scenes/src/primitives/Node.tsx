import { glowRings, solidRings } from "../api/glow";
import { attention, surface, text, type AttentionLevel } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface NodeProps {
  /** Centre, in pixels. */
  x: number;
  y: number;
  width: number;
  height: number;
  label: string;
  sublabel?: string;
  /** State colour — always an attention level, never a minted colour. */
  level: AttentionLevel;
  /** 0..1 entrance (fade + rise). */
  appear?: number;
  /** 0..1 highlight (glow + scale) for the active node. */
  emphasis?: number;
  unit: number;
}

/** A process step: a labelled box coloured by who must act. */
export function Node({
  x,
  y,
  width,
  height,
  label,
  sublabel,
  level,
  appear = 1,
  emphasis = 0,
  unit,
}: NodeProps) {
  const palette = attention[level];
  const scale = 1 + 0.06 * emphasis;
  const rise = (1 - appear) * 16 * unit;
  return (
    <div
      data-studio-node={label}
      data-level={level}
      style={{
        position: "absolute",
        left: x - width / 2,
        top: y - height / 2,
        width,
        height,
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: Math.round(4 * unit),
        borderRadius: Math.round(14 * unit),
        // Opaque underlay so edges drawn beneath never show through.
        backgroundColor: surface.raised,
        backgroundImage: `linear-gradient(${palette.bg}, ${palette.bg})`,
        border: `${Math.max(1, Math.round(2 * unit))}px solid ${palette.border}`,
        // Blur-free glow: solid rings (see src/api/glow.ts for why no blur).
        boxShadow: solidRings(glowRings(18 * unit, emphasis, palette.bg, 5)),
        opacity: appear,
        transform: `translateY(${rise}px) scale(${scale})`,
        fontFamily: fontStack,
        textAlign: "center",
      }}
    >
      <div
        style={{
          color: level === "inert" ? text.muted : palette.fg,
          fontSize: Math.round(typeScale.label * unit),
          fontWeight: fontWeights.semibold,
          lineHeight: 1.15,
        }}
      >
        {label}
      </div>
      {sublabel ? (
        <div
          style={{
            color: text.muted,
            fontSize: Math.round(typeScale.caption * 0.85 * unit),
            lineHeight: 1.1,
          }}
        >
          {sublabel}
        </div>
      ) : null}
    </div>
  );
}
