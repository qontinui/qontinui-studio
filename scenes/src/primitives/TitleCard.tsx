import type { Layout } from "../api/layout";
import { durations, msToFrames, progress } from "../api";
import { brand, text } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface TitleCardProps {
  title: string;
  body?: string;
  layout: Layout;
  /** Frame within the card (its entrance is driven by it). */
  local: number;
  fps: number;
}

/**
 * A composition's title or closing card: a centred title, an optional line
 * of body text and a brand rule. Text is bounded by `CARD_TITLE_MAX` /
 * `CARD_BODY_MAX` at resolution and wraps inside the title-safe width.
 */
export function TitleCard({ title, body, layout, local, fps }: TitleCardProps) {
  const u = layout.unit;
  const appear = progress(local, 0, msToFrames(durations.slow, fps));
  return (
    <div
      data-studio-card={title}
      style={{
        position: "absolute",
        left: layout.safe.x,
        top: layout.safe.y,
        width: layout.safe.width,
        height: layout.safe.height,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        fontFamily: fontStack,
        opacity: appear,
        transform: `translateY(${(1 - appear) * 16 * u}px)`,
      }}
    >
      <div
        style={{
          color: text.primary,
          fontSize: Math.round(typeScale.display * u),
          fontWeight: fontWeights.bold,
          lineHeight: 1.1,
          maxWidth: layout.safe.width,
          overflowWrap: "anywhere",
        }}
      >
        {title}
      </div>
      <div
        style={{
          width: Math.round(120 * u),
          height: Math.max(2, Math.round(4 * u)),
          background: brand.primary,
          borderRadius: Math.round(2 * u),
          marginTop: Math.round(28 * u),
        }}
      />
      {body ? (
        <div
          style={{
            color: text.secondary,
            fontSize: Math.round(typeScale.title * 0.75 * u),
            fontWeight: fontWeights.regular,
            lineHeight: 1.35,
            marginTop: Math.round(28 * u),
            maxWidth: Math.round(layout.safe.width * 0.85),
            overflowWrap: "anywhere",
          }}
        >
          {body}
        </div>
      ) : null}
    </div>
  );
}
