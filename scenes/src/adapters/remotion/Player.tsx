/**
 * Web embed of a studio scene through `@remotion/player`. The same scene code
 * that renders the video renders here (plan D1). Consumers serve the
 * `assets/fonts/` directory and pass its parent as `assetBaseUrl`.
 *
 * The trace is NOT taken on trust: it is validated against the schema, the
 * scene's requirements are checked, and a replay without build-record
 * provenance is refused (plan D2) — any failure renders an error panel
 * instead of the scene.
 *
 * Reduced motion (plan Phase 3): see `PlayerEmbedProps`. Under
 * `prefers-reduced-motion: reduce` the player never autoplays; it opens on
 * the scene's poster frame with its play control.
 *
 * Licence note (plan Phase 0): Remotion is source-available, not OSI.
 * `acknowledgeRemotionLicense` records that the free-licence terms were
 * reviewed (Phase 0); an AGPL consumer (qontinui-web) embeds this only under
 * its §7 additional permission, loaded as a separate lazy chunk.
 */
import { Player } from "@remotion/player";
import { useMemo, useSyncExternalStore } from "react";

import type { PlayerEmbedProps } from "../../api/engine";
import { ASPECT_SIZES, STUDIO_FPS } from "../../api/layout";
import { missingRequirements } from "../../api/scene";
import { posterFrame, sceneDuration } from "../../api/scene-root";
import { attention, surface } from "../../api/tokens";
import { renderRefusal } from "../../api/trace/provenance";
import { validateTrace, type SceneTrace } from "../../api/trace/schema";
import { fontStack } from "../../api/typography";
import { scenes } from "../../scenes";
import { StudioComposition, type StudioCompositionProps } from "./StudioComposition";

type Checked =
  | { ok: true; trace: SceneTrace; duration: number; poster: number }
  | { ok: false; errors: string[] };

const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

function subscribeReducedMotion(onChange: () => void): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => undefined;
  const query = window.matchMedia(REDUCED_MOTION);
  // Safari before 14 has only the deprecated addListener/removeListener.
  if (typeof query.addEventListener !== "function") {
    query.addListener(onChange);
    return () => query.removeListener(onChange);
  }
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/** True unless the browser positively reports that motion is fine. */
function reducedMotionNow(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return true;
  return window.matchMedia(REDUCED_MOTION).matches;
}

/** Server render: the preference is unknown, so assume reduced (never autoplay). */
const reducedMotionOnServer = () => true;

/** `prefers-reduced-motion: reduce`, live; true while it cannot be known. */
export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(subscribeReducedMotion, reducedMotionNow, reducedMotionOnServer);
}

function check(sceneId: string, input: unknown): Checked {
  const scene = scenes[sceneId];
  if (!scene) return { ok: false, errors: [`unknown scene "${sceneId}"`] };
  const result = validateTrace(input);
  if (!result.ok) return { ok: false, errors: result.errors };
  // validateTrace returns a fresh copy, so provenance is checked on the
  // caller's object: only a trace built by fromBuildRecord may be a replay.
  const refusal = result.trace.meta.mode === "replay" ? renderRefusal(input as SceneTrace) : null;
  if (refusal) return { ok: false, errors: [refusal] };
  const problems = missingRequirements(scene, result.trace);
  if (problems.length) return { ok: false, errors: [`trace cannot drive ${sceneId}; it needs: ${problems.join("; ")}`] };
  try {
    return {
      ok: true,
      trace: result.trace,
      duration: sceneDuration(scene, result.trace),
      poster: posterFrame(scene, result.trace),
    };
  } catch (err) {
    // A scene bug (e.g. a poster outside the scene) shows the error panel,
    // never crashes the consumer's page.
    return { ok: false, errors: [err instanceof Error ? err.message : String(err)] };
  }
}

export function StudioPlayer({
  scene,
  trace,
  aspect,
  assetBaseUrl,
  autoPlay = false,
  loop = false,
  controls = true,
  className,
  style,
}: PlayerEmbedProps) {
  const { width, height } = ASPECT_SIZES[aspect];
  const checked = useMemo(() => check(scene, trace), [scene, trace]);
  const reducedMotion = usePrefersReducedMotion();
  const box = { width: "100%", aspectRatio: `${width} / ${height}`, ...style };

  if (!checked.ok) {
    return (
      <div
        role="alert"
        data-studio-player-error
        className={className}
        style={{
          ...box,
          boxSizing: "border-box",
          padding: 16,
          overflow: "auto",
          background: surface.canvas,
          color: attention.attention.fg,
          border: `1px solid ${attention.attention.border}`,
          fontFamily: fontStack,
          fontSize: 14,
        }}
      >
        <strong>Scene cannot be shown.</strong>
        <ul>
          {checked.errors.map((error) => (
            <li key={error}>{error}</li>
          ))}
        </ul>
      </div>
    );
  }

  const inputProps: StudioCompositionProps = { sceneId: scene, trace: checked.trace, aspect, assetBaseUrl };
  // Reduced motion: never autoplay; open on the poster frame with a play
  // control. Remotion reads autoPlay and initialFrame only on mount, so the
  // key remounts the player when the preference changes (e.g. after
  // hydration, where the server assumed reduced motion).
  const playsOnItsOwn = autoPlay && !reducedMotion;
  return (
    <Player
      key={playsOnItsOwn ? "motion" : "still"}
      initialFrame={playsOnItsOwn ? 0 : checked.poster}
      component={StudioComposition}
      inputProps={inputProps}
      durationInFrames={checked.duration}
      fps={STUDIO_FPS}
      compositionWidth={width}
      compositionHeight={height}
      autoPlay={playsOnItsOwn}
      loop={loop}
      controls={controls || reducedMotion}
      className={className}
      style={box}
      acknowledgeRemotionLicense
    />
  );
}
