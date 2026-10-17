import { border, surface, text } from "../api/tokens";
import { fontStack, typeScale } from "../api/typography";

export interface LabelProps {
  /** Centre, in pixels. */
  x: number;
  y: number;
  text: string;
  /** Widest it may get before it ellipsises, in pixels. */
  maxWidth: number;
  appear?: number;
  unit: number;
}

/**
 * A small one-line label on a canvas-coloured pill, centred on a point: an
 * edge's action, readable over the line it sits on.
 */
export function Label({ x, y, text: content, maxWidth, appear = 1, unit }: LabelProps) {
  const height = Math.round(30 * unit);
  // Never narrower than a short word, whatever room the caller computed.
  const width = Math.max(Math.round(80 * unit), Math.round(maxWidth));
  return (
    <div
      data-studio-label={content}
      style={{
        position: "absolute",
        left: Math.round(x - width / 2),
        top: Math.round(y - height / 2),
        width,
        height,
        display: "flex",
        justifyContent: "center",
        opacity: appear,
      }}
    >
      <span
        style={{
          maxWidth: "100%",
          boxSizing: "border-box",
          padding: `${Math.round(3 * unit)}px ${Math.round(10 * unit)}px`,
          background: surface.canvas,
          border: `${Math.max(1, Math.round(1 * unit))}px solid ${border.subtle}`,
          borderRadius: height,
          color: text.secondary,
          fontFamily: fontStack,
          fontSize: Math.round(typeScale.caption * 0.85 * unit),
          lineHeight: 1.2,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {content}
      </span>
    </div>
  );
}
