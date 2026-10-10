/**
 * Browser entry of a studio HyperFrames composition (bundled by
 * ./project.ts into `studio-scene.js`). It reads the render config the
 * adapter embedded in `index.html`, loads the bundled Inter, draws the first
 * frame, and only then registers the frame adapter with the HyperFrames
 * runtime — so capture cannot start before the fonts are in.
 *
 * Registration seam, by HyperFrames version:
 * - The pinned 0.8.78 runtime drives `window.__timelines[<composition id>]`.
 *   An entry may be a PARTIAL timeline ("duration/seek only … render fine",
 *   runtime/init.ts), so the adapter is exposed as `{ duration, seek }` plus
 *   no-op transport methods. `seek(t)` maps seconds back to the frame index
 *   HyperFrames derived them from (`t = floor(frame) / fps`).
 * - 0.8.140+ adds `window.__hyperframes.registerFrameSource`, the documented
 *   way to drive scene code that is a function of time. Moving to it is a
 *   change to this file only.
 */
import { createRoot } from "react-dom/client";

import { isAspect } from "../../api/layout";
import { parseTrace } from "../../api/trace/schema";
import { getScene } from "../../scenes";
import { loadStudioFonts } from "../fonts";
import { createStudioFrameAdapter } from "./frame-adapter";
import { STUDIO_CONFIG_ELEMENT_ID, type StudioPageConfig } from "./page-config";

type PartialTimeline = {
  duration: () => number;
  time: () => number;
  seek: (timeSeconds?: number) => void;
  pause: () => void;
  play: () => void;
  paused: (paused?: boolean) => boolean;
};

declare global {
  interface Window {
    __timelines?: Record<string, unknown>;
  }
}

function readConfig(): StudioPageConfig {
  const element = document.getElementById(STUDIO_CONFIG_ELEMENT_ID);
  if (!element?.textContent) throw new Error(`missing #${STUDIO_CONFIG_ELEMENT_ID}`);
  const config = JSON.parse(element.textContent) as StudioPageConfig;
  if (!isAspect(config.aspect)) throw new Error(`bad aspect ${String(config.aspect)}`);
  return config;
}

async function main(): Promise<void> {
  const config = readConfig();
  const scene = getScene(config.sceneId);
  const trace = parseTrace(config.trace);
  const mount = document.getElementById(config.mountId);
  if (!mount) throw new Error(`missing #${config.mountId}`);

  await loadStudioFonts("hyperframes", (file) => file);
  await document.fonts.ready;

  const adapter = createStudioFrameAdapter({
    id: config.compositionId,
    root: createRoot(mount),
    scene,
    trace,
    aspect: config.aspect,
    frames: config.frames,
  });
  await adapter.init?.({
    compositionId: config.compositionId,
    fps: config.fps,
    width: config.width,
    height: config.height,
    rootElement: mount,
  });
  const durationFrames = adapter.getDurationFrames();
  let current = 0;
  await adapter.seekFrame(0);

  // Playback clock, for the <hyperframes-player> embed only. A render never
  // calls play(): it pauses and seeks. The clock picks WHICH frame to show;
  // the frame itself is still a pure function of (trace, frame).
  let playing: { startFrame: number; startMs: number; handle: number } | null = null;
  const tick = (now: number) => {
    if (!playing) return;
    const frame = playing.startFrame + Math.floor(((now - playing.startMs) / 1000) * config.fps);
    const clamped = Math.min(durationFrames - 1, frame);
    if (clamped !== current) {
      current = clamped;
      void adapter.seekFrame(current);
    }
    if (frame >= durationFrames - 1) {
      playing = null;
      return;
    }
    playing.handle = requestAnimationFrame(tick);
  };
  const stop = () => {
    if (playing) cancelAnimationFrame(playing.handle);
    playing = null;
  };

  const timeline: PartialTimeline = {
    duration: () => durationFrames / config.fps,
    time: () => current / config.fps,
    seek: (timeSeconds = 0) => {
      const frame = Math.round(Math.max(0, Number(timeSeconds) || 0) * config.fps);
      current = Math.min(durationFrames - 1, frame);
      if (playing) playing = { ...playing, startFrame: current, startMs: performance.now() };
      void adapter.seekFrame(current);
    },
    pause: stop,
    play: () => {
      if (playing) return;
      const startFrame = current >= durationFrames - 1 ? 0 : current;
      playing = { startFrame, startMs: performance.now(), handle: requestAnimationFrame(tick) };
    },
    paused: () => playing === null,
  };
  window.__timelines = window.__timelines ?? {};
  window.__timelines[config.compositionId] = timeline;
}

main().catch((err: unknown) => {
  // Surface the failure in the page so the render log shows it.
  document.body.setAttribute("data-studio-error", err instanceof Error ? err.message : String(err));
  throw err;
});
