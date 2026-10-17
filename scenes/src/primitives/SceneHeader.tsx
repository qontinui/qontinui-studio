import { text } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface SceneHeaderProps {
  /** Top-left, in pixels. Start it below the mode label's room (see ModeLabel). */
  x: number;
  y: number;
  width: number;
  /** Small upper-case line above the title, e.g. "THE DIGITAL TWIN". */
  eyebrow: string;
  title: string;
  /** Title size in reference px (default the display-ish 48). */
  titleSize?: number;
  unit: number;
}

/**
 * A scene's header: an eyebrow and a title clamped to two lines whatever its
 * length, so the layout below can reserve exactly that much room.
 */
export function SceneHeader({ x, y, width, eyebrow, title, titleSize = 48, unit }: SceneHeaderProps) {
  return (
    <div data-studio-header={eyebrow} style={{ position: "absolute", left: x, top: y, width, fontFamily: fontStack }}>
      <div
        style={{
          color: text.muted,
          fontSize: Math.round(typeScale.label * unit),
          fontWeight: fontWeights.semibold,
          letterSpacing: "0.12em",
        }}
      >
        {eyebrow}
      </div>
      <div
        style={{
          color: text.primary,
          fontSize: Math.round(titleSize * unit),
          fontWeight: fontWeights.bold,
          lineHeight: 1.1,
          marginTop: Math.round(8 * unit),
          display: "-webkit-box",
          WebkitBoxOrient: "vertical",
          WebkitLineClamp: 2,
          overflow: "hidden",
          overflowWrap: "anywhere",
        }}
      >
        {title}
      </div>
    </div>
  );
}
