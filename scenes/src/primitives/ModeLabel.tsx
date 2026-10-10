import { modeLine } from "../api/format";
import type { Layout } from "../api/layout";
import { border, brand, glow, surface, text } from "../api/tokens";
import type { SceneTrace } from "../api/trace/schema";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface ModeLabelProps {
  meta: SceneTrace["meta"];
  layout: Layout;
}

/**
 * Plan D2: every rendered output states whether it is illustrative or a
 * replay; a replay also states its date range and source. `SceneRoot`
 * renders this above the scene on every frame, so a scene cannot leave it out
 * or cover it.
 *
 * The detail line wraps inside the title-safe width instead of being clipped;
 * `SOURCE_REF_MAX` in the schema bounds it to three lines at 9:16.
 *
 * Badge colours are neutral (border / brand tokens), not attention-layer
 * state colours: the mode says what the footage IS, not who must act.
 */
export function ModeLabel({ meta, layout }: ModeLabelProps) {
  const { badge, detail } = modeLine(meta);
  const u = layout.unit;
  const badgeColors =
    meta.mode === "replay"
      ? { bg: glow.primary, fg: text.primary, border: brand.primary }
      : { bg: surface.hover, fg: text.secondary, border: border.strong };
  const line = Math.max(1, Math.round(1.5 * u));
  return (
    <div
      data-studio-mode={meta.mode}
      style={{
        position: "absolute",
        left: layout.safe.x,
        top: Math.round(28 * u),
        maxWidth: layout.safe.width,
        boxSizing: "border-box",
        display: "flex",
        alignItems: "flex-start",
        gap: Math.round(12 * u),
        padding: `${Math.round(8 * u)}px ${Math.round(14 * u)}px`,
        background: surface.raised,
        border: `${line}px solid ${border.default}`,
        borderRadius: Math.round(10 * u),
        fontFamily: fontStack,
        fontSize: Math.round(typeScale.caption * u),
        lineHeight: 1.3,
        color: text.secondary,
      }}
    >
      <span
        style={{
          flex: "none",
          padding: `${Math.round(2 * u)}px ${Math.round(10 * u)}px`,
          borderRadius: Math.round(6 * u),
          background: badgeColors.bg,
          color: badgeColors.fg,
          border: `${line}px solid ${badgeColors.border}`,
          fontWeight: fontWeights.bold,
          letterSpacing: "0.08em",
          whiteSpace: "nowrap",
        }}
      >
        {badge}
      </span>
      <span
        data-studio-mode-detail
        style={{
          minWidth: 0,
          paddingTop: Math.round(3 * u),
          whiteSpace: "normal",
          overflowWrap: "anywhere",
        }}
      >
        {detail}
      </span>
    </div>
  );
}
