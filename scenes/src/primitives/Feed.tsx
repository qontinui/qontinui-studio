import { attention, border, surface, text, type AttentionLevel } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

export interface FeedRow {
  /** React key; must be unique within the feed. */
  id: string;
  text: string;
  /** Optional second line, muted (e.g. where the row came from). */
  detail?: string;
  /** Dot colour — an attention level. */
  level: AttentionLevel;
  /** 0..1 entrance (fade + slide). */
  appear: number;
}

export interface FeedProps {
  /** Top-left, in pixels. */
  x: number;
  y: number;
  width: number;
  /** Panel heading, upper case. */
  title: string;
  /** The rows to show, oldest first; the caller keeps the list short. */
  rows: readonly FeedRow[];
  /** Rows of room reserved, so the panel does not grow as rows arrive. */
  capacity: number;
  /** Shown when there are no rows yet. */
  empty?: string;
  appear?: number;
  unit: number;
}

const ROW = 58;
const PAD = 16;
const HEADING = 36;

/** Height in pixels of a Feed with room for `capacity` rows, for layouts. */
export function feedHeight(capacity: number, unit: number): number {
  return Math.round(HEADING * unit) + 2 * Math.round(PAD * unit) + capacity * Math.round(ROW * unit);
}

/**
 * A titled panel of short rows, each with a state dot: a log of what has
 * happened (facts mirrored into the twin, claims taken, a diagnosis). Text is
 * one line per row, ellipsised, so the panel's height is fixed by `capacity`.
 */
export function Feed({ x, y, width, title, rows, capacity, empty, appear = 1, unit }: FeedProps) {
  const rowHeight = Math.round(ROW * unit);
  const pad = Math.round(PAD * unit);
  const heading = Math.round(HEADING * unit);
  const dot = Math.round(12 * unit);
  const line = Math.max(1, Math.round(1.5 * unit));
  return (
    <div
      data-studio-feed={title}
      style={{
        position: "absolute",
        left: x,
        top: y,
        width,
        height: feedHeight(capacity, unit),
        boxSizing: "border-box",
        padding: pad,
        background: surface.raised,
        border: `${line}px solid ${border.default}`,
        borderRadius: Math.round(14 * unit),
        opacity: appear,
        fontFamily: fontStack,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          height: heading,
          color: text.muted,
          fontSize: Math.round(typeScale.caption * unit),
          fontWeight: fontWeights.semibold,
          letterSpacing: "0.12em",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {title}
      </div>
      {rows.length === 0 && empty ? (
        <div style={{ color: text.muted, fontSize: Math.round(typeScale.caption * unit) }}>{empty}</div>
      ) : null}
      {rows.map((row) => {
        const palette = attention[row.level];
        return (
          <div
            key={row.id}
            data-studio-feed-row={row.text}
            data-level={row.level}
            style={{
              height: rowHeight,
              display: "flex",
              alignItems: "flex-start",
              gap: Math.round(12 * unit),
              opacity: row.appear,
              transform: `translateX(${(1 - row.appear) * 16 * unit}px)`,
            }}
          >
            <div
              style={{
                flex: "none",
                width: dot,
                height: dot,
                marginTop: Math.round(6 * unit),
                borderRadius: dot,
                boxSizing: "border-box",
                background: palette.bg,
                border: `${Math.max(1, Math.round(2 * unit))}px solid ${palette.fg}`,
              }}
            />
            <div style={{ minWidth: 0, flex: 1 }}>
              <div
                style={{
                  color: text.primary,
                  fontSize: Math.round(typeScale.caption * unit),
                  fontWeight: fontWeights.semibold,
                  lineHeight: 1.25,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {row.text}
              </div>
              {row.detail ? (
                <div
                  style={{
                    color: text.muted,
                    fontSize: Math.round(typeScale.caption * 0.85 * unit),
                    lineHeight: 1.25,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {row.detail}
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}
