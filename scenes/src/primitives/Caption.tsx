import { text } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface CaptionProps {
  /** Top-left of the caption box, in pixels. */
  x: number;
  y: number;
  width: number;
  title: string;
  body?: string;
  /** 0..1 entrance (fade + slide). */
  appear?: number;
  align?: "left" | "center";
  /** Clamp the body to this many lines with an ellipsis (default: no clamp). */
  maxBodyLines?: number;
  unit: number;
}

/** Narrative caption: a short title and an optional line of body text. */
export function Caption({ x, y, width, title, body, appear = 1, align = "left", maxBodyLines, unit }: CaptionProps) {
  const clamp =
    maxBodyLines === undefined
      ? {}
      : { display: "-webkit-box", WebkitBoxOrient: "vertical" as const, WebkitLineClamp: maxBodyLines, overflow: "hidden" };
  return (
    <div
      data-studio-caption={title}
      style={{
        position: "absolute",
        left: x,
        top: y,
        width,
        textAlign: align,
        opacity: appear,
        transform: `translateY(${(1 - appear) * 12 * unit}px)`,
        fontFamily: fontStack,
      }}
    >
      <div
        style={{
          color: text.primary,
          fontSize: Math.round(typeScale.title * unit),
          fontWeight: fontWeights.bold,
          lineHeight: 1.15,
        }}
      >
        {title}
      </div>
      {body ? (
        <div
          style={{
            color: text.secondary,
            fontSize: Math.round(typeScale.body * unit),
            fontWeight: fontWeights.regular,
            lineHeight: 1.35,
            marginTop: Math.round(10 * unit),
            ...clamp,
          }}
        >
          {body}
        </div>
      ) : null}
    </div>
  );
}
