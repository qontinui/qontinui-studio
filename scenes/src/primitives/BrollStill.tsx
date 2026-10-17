import type { ComponentType } from "react";

import type { Layout } from "../api/layout";
import { border, surface, text } from "../api/tokens";
import { fontStack, fontWeights, typeScale } from "../api/typography";

/** How an adapter draws an image so the frame waits for it to load. */
export type ImageComponent = ComponentType<{ src: string; style: Record<string, string | number> }>;

export interface BrollStillProps {
  src: string;
  caption?: string;
  layout: Layout;
  /** The adapter's image component (e.g. one that delays the frame until decoded). */
  Image: ImageComponent;
}

/** The on-image label every AI b-roll still carries (sibling plan D6). */
export const AI_IMAGERY_LABEL = "AI-GENERATED IMAGERY";

/**
 * An AI b-roll still, full bleed, with an "AI-generated" badge drawn into the
 * frame: AI imagery is never presented as the real thing (sibling plan D6).
 * Diagrams, numbers and UI stay in the scenes, drawn from data (plan D3).
 */
export function BrollStill({ src, caption, layout, Image }: BrollStillProps) {
  const u = layout.unit;
  const line = Math.max(1, Math.round(1.5 * u));
  return (
    <div data-studio-broll={src} style={{ position: "absolute", inset: 0, background: surface.canvas }}>
      <Image src={src} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" }} />
      <div
        style={{
          position: "absolute",
          right: layout.safe.x,
          bottom: layout.safe.y,
          maxWidth: Math.round(layout.safe.width * 0.8),
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-end",
          gap: Math.round(8 * u),
          fontFamily: fontStack,
        }}
      >
        <span
          data-studio-ai-label
          style={{
            padding: `${Math.round(4 * u)}px ${Math.round(12 * u)}px`,
            background: surface.raised,
            border: `${line}px solid ${border.strong}`,
            borderRadius: Math.round(6 * u),
            color: text.primary,
            fontSize: Math.round(typeScale.caption * u),
            fontWeight: fontWeights.bold,
            letterSpacing: "0.08em",
            whiteSpace: "nowrap",
          }}
        >
          {AI_IMAGERY_LABEL}
        </span>
        {caption ? (
          <span
            style={{
              padding: `${Math.round(4 * u)}px ${Math.round(12 * u)}px`,
              background: surface.raised,
              borderRadius: Math.round(6 * u),
              color: text.secondary,
              fontSize: Math.round(typeScale.caption * u),
              textAlign: "right",
              overflowWrap: "anywhere",
            }}
          >
            {caption}
          </span>
        ) : null}
      </div>
    </div>
  );
}
