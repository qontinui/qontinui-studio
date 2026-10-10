import { text } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface CounterProps {
  /** Top-left, in pixels. */
  x: number;
  y: number;
  label: string;
  /** Already-formatted value; the caller interpolates and formats it. */
  value: string;
  align?: "left" | "right";
  appear?: number;
  unit: number;
}

/** A big number with a label, in tabular figures so digits do not jitter. */
export function Counter({ x, y, label, value, align = "left", appear = 1, unit }: CounterProps) {
  return (
    <div
      data-studio-counter={label}
      style={{
        position: "absolute",
        left: align === "left" ? x : undefined,
        right: align === "right" ? x : undefined,
        top: y,
        textAlign: align,
        opacity: appear,
        fontFamily: fontStack,
      }}
    >
      <div
        style={{
          color: text.primary,
          fontSize: Math.round(typeScale.counter * unit),
          fontWeight: fontWeights.bold,
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1,
        }}
      >
        {value}
      </div>
      <div
        style={{
          color: text.muted,
          fontSize: Math.round(typeScale.caption * unit),
          marginTop: Math.round(6 * unit),
          letterSpacing: "0.04em",
        }}
      >
        {label}
      </div>
    </div>
  );
}
