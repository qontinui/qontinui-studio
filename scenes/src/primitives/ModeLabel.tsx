import { modeLine } from "../api/format";
import type { Layout } from "../api/layout";
import { attention, border, surface, text } from "../api/tokens";
import type { SceneTrace } from "../api/trace/schema";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface ModeLabelProps {
  meta: SceneTrace["meta"];
  layout: Layout;
}

/**
 * Plan D2: every rendered output states whether it is illustrative or a
 * replay; a replay also states its date range and source. `SceneFrame`
 * renders this on every frame, so a scene cannot leave it out.
 */
export function ModeLabel({ meta, layout }: ModeLabelProps) {
  const { badge, detail } = modeLine(meta);
  const u = layout.unit;
  const badgeColors = meta.mode === "replay" ? attention.landing : attention.inert;
  return (
    <div
      data-studio-mode={meta.mode}
      style={{
        position: "absolute",
        left: layout.safe.x,
        top: Math.round(28 * u),
        display: "flex",
        alignItems: "center",
        gap: Math.round(12 * u),
        padding: `${Math.round(8 * u)}px ${Math.round(14 * u)}px`,
        background: surface.raised,
        border: `${Math.max(1, Math.round(1.5 * u))}px solid ${border.default}`,
        borderRadius: Math.round(10 * u),
        fontFamily: fontStack,
        fontSize: Math.round(typeScale.caption * u),
        lineHeight: 1.2,
        color: text.secondary,
        whiteSpace: "nowrap",
      }}
    >
      <span
        style={{
          padding: `${Math.round(3 * u)}px ${Math.round(10 * u)}px`,
          borderRadius: Math.round(6 * u),
          background: badgeColors.bg,
          color: badgeColors.fg,
          border: `${Math.max(1, Math.round(1.5 * u))}px solid ${badgeColors.border}`,
          fontWeight: fontWeights.bold,
          letterSpacing: "0.08em",
        }}
      >
        {badge}
      </span>
      <span>{detail}</span>
    </div>
  );
}
