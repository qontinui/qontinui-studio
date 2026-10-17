/**
 * The Phase 3 scene matrix (plan arming line: "per-scene render matrix (16:9,
 * 9:16, 1:1) passes and asserts the on-screen D2 mode label").
 *
 * For every registered scene, at each aspect in `SCENE_MATRIX_ASPECTS`
 * (comma-separated; default all three — CI runs one job per aspect):
 *
 * - **Render:** the whole scene renders to mp4 through the engine adapter at
 *   the aspect's native size, with the duration its timeline gives, and its
 *   poster frame renders as a still.
 * - **Embed:** the adapter's web player mounts the scene in the embed test
 *   page (`tests/embed/`), from the scene's committed illustrative trace, and
 *   plays when asked to autoplay.
 * - **D2 label, on screen:** in the embed, the mode label shows the trace's
 *   own mode line (`modeLine`): ILLUSTRATIVE for the illustrative trace;
 *   REPLAY plus date range and source for the same events as an attested
 *   replay. "On screen" means: inside the scene's frame, non-empty, opaque,
 *   visible, and the topmost scene element at its centre.
 * - **Reduced motion:** under `prefers-reduced-motion: reduce` the embed,
 *   even when asked to autoplay and given no controls, stays on the scene's
 *   poster frame, shows a play control, and plays only once it is pressed.
 *   Without the preference, an embed not asked to autoplay also opens on
 *   the poster frame and stays there.
 */
import { mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { ensureBrowser } from "@remotion/renderer";
import puppeteer, { type Browser, type Page } from "puppeteer-core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { RemotionAdapter } from "../../src/adapters/remotion/render";
import { ASPECTS, ASPECT_SIZES, isAspect, type Aspect } from "../../src/api/layout";
import { modeLine } from "../../src/api/format";
import { posterFrame, sceneDuration } from "../../src/api/scene-root";
import { scenes } from "../../src/scenes";
import { loadIllustrativeTrace } from "../golden/cases";
import { embedReplayMeta } from "../embed/replay-meta";
import { serveEmbedPage, type EmbedServer } from "../embed/serve";

function matrixAspects(): Aspect[] {
  const raw = process.env.SCENE_MATRIX_ASPECTS;
  if (!raw) return [...ASPECTS];
  const aspects = raw.split(",").map((a) => a.trim());
  for (const a of aspects) if (!isAspect(a)) throw new Error(`SCENE_MATRIX_ASPECTS: unknown aspect "${a}"`);
  return aspects as Aspect[];
}

const ASPECTS_UNDER_TEST = matrixAspects();
const SCENE_IDS = Object.keys(scenes).sort();
const CELLS = SCENE_IDS.flatMap((scene) => ASPECTS_UNDER_TEST.map((aspect) => ({ scene, aspect })));

/** Long enough for several frames at 30 fps even on a loaded runner. */
const PLAY_WAIT_MS = 1500;
const LOAD_TIMEOUT_MS = 60_000;

let server: EmbedServer;
let browser: Browser;
let serveUrl: string;
let outDir: string;

beforeAll(async () => {
  // The same pinned Chrome Headless Shell the renders use.
  const status = await ensureBrowser({ logLevel: "error" });
  if (!("path" in status)) throw new Error(`no usable browser: ${JSON.stringify(status)}`);
  browser = await puppeteer.launch({
    executablePath: status.path,
    headless: "shell",
    // Local test pages only; the CI runner's AppArmor refuses the sandbox.
    args: ["--no-sandbox"],
  });
  server = await serveEmbedPage();
  serveUrl = await RemotionAdapter.bundle();
  outDir = mkdtempSync(path.join(tmpdir(), "studio-matrix-"));
}, 10 * 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.close();
  if (outDir) rmSync(outDir, { recursive: true, force: true });
});

interface EmbedOptions {
  scene: string;
  aspect: Aspect;
  mode?: "illustrative" | "replay";
  autoplay: boolean;
  controls?: boolean;
  reducedMotion?: boolean;
}

async function openEmbed(options: EmbedOptions): Promise<Page> {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 1000 });
  if (options.reducedMotion) await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
  const query = new URLSearchParams({
    scene: options.scene,
    aspect: options.aspect,
    mode: options.mode ?? "illustrative",
    autoplay: options.autoplay ? "1" : "0",
    controls: options.controls === false ? "0" : "1",
  });
  await page.goto(`${server.url}/?${query.toString()}`);
  await page.waitForFunction(
    () => document.body.dataset.embedError !== undefined || document.querySelector("[data-studio-scene]") !== null,
    { timeout: LOAD_TIMEOUT_MS },
  );
  const error = await page.evaluate(
    () => document.body.dataset.embedError ?? document.querySelector("[data-studio-player-error]")?.textContent ?? null,
  );
  if (error) throw new Error(`embed of ${options.scene} failed: ${error}`);
  // Fonts are part of the frame; wait until they are in.
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  return page;
}

const frameOf = (page: Page) =>
  page.$eval("[data-studio-scene]", (el) => Number((el as HTMLElement).dataset.studioFrame));

interface LabelReading {
  mode: string | null;
  badge: string;
  detail: string;
  inside: boolean;
  size: { width: number; height: number };
  visible: boolean;
  topmost: boolean;
}

/** What the mode label shows, and whether it is really on screen. */
function readLabel(page: Page): Promise<LabelReading> {
  return page.evaluate(() => {
    const root = document.querySelector("[data-studio-scene]") as HTMLElement;
    const label = root.querySelector("[data-studio-mode]") as HTMLElement | null;
    if (!label) throw new Error("no [data-studio-mode] label in the scene");
    const r = root.getBoundingClientRect();
    const l = label.getBoundingClientRect();
    let visible = true;
    for (let el: HTMLElement | null = label; el && el !== root.parentElement; el = el.parentElement) {
      const style = getComputedStyle(el);
      if (style.visibility !== "visible" || style.display === "none" || Number(style.opacity) < 1) visible = false;
    }
    // The label layer takes no pointer events (it must not block the
    // player's controls), so hit-testing needs it switched on for a moment.
    const layer = label.parentElement as HTMLElement;
    const previous = layer.style.pointerEvents;
    layer.style.pointerEvents = "auto";
    const hits = document.elementsFromPoint((l.left + l.right) / 2, (l.top + l.bottom) / 2);
    layer.style.pointerEvents = previous;
    const topInScene = hits.find((el) => root.contains(el));
    return {
      mode: label.dataset.studioMode ?? null,
      badge: (label.firstElementChild?.textContent ?? "").trim(),
      detail: (label.querySelector("[data-studio-mode-detail]")?.textContent ?? "").trim(),
      inside: l.left >= r.left - 0.5 && l.top >= r.top - 0.5 && l.right <= r.right + 0.5 && l.bottom <= r.bottom + 0.5,
      size: { width: l.width, height: l.height },
      visible,
      topmost: topInScene !== undefined && label.contains(topInScene),
    };
  });
}

function expectLabelOnScreen(reading: LabelReading, expected: { mode: string; badge: string; detail: string }) {
  expect(reading.mode).toBe(expected.mode);
  expect(reading.badge).toBe(expected.badge);
  expect(reading.detail).toBe(expected.detail);
  expect(reading.inside, "the mode label lies inside the scene frame").toBe(true);
  expect(reading.size.width).toBeGreaterThan(0);
  expect(reading.size.height).toBeGreaterThan(0);
  expect(reading.visible, "the mode label is visible and opaque").toBe(true);
  expect(reading.topmost, "nothing in the scene covers the mode label").toBe(true);
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe.each(CELLS)("scene matrix: $scene at $aspect", ({ scene, aspect }) => {
  const definition = scenes[scene];
  if (!definition) throw new Error(`unknown scene ${scene}`);
  const trace = loadIllustrativeTrace(scene);
  const poster = posterFrame(definition, trace);
  const duration = sceneDuration(definition, trace);

  it("renders the whole scene to mp4 and its poster frame as a still", async () => {
    const adapter = new RemotionAdapter({ serveUrl });
    const dir = path.join(outDir, `${scene}-${aspect.replace(":", "x")}`);
    try {
      const result = await adapter.render({ scene, trace, format: "mp4", aspect, outPath: path.join(dir, "scene.mp4") });
      expect(result.width).toBe(ASPECT_SIZES[aspect].width);
      expect(result.height).toBe(ASPECT_SIZES[aspect].height);
      expect(result.durationInFrames).toBe(duration);
      expect(statSync(result.files[0] as string).size).toBeGreaterThan(0);
      const [still] = await adapter.renderStills({ scene, trace, aspect, frames: [poster], outDir: dir, prefix: "poster-" });
      expect(statSync(still as string).size).toBeGreaterThan(0);
    } finally {
      await adapter.close();
    }
  });

  it("embeds, plays, and shows the ILLUSTRATIVE label on screen", async () => {
    const page = await openEmbed({ scene, aspect, autoplay: true });
    try {
      const first = await frameOf(page);
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(page), "an autoplaying embed advances").not.toBe(first);
      expectLabelOnScreen(await readLabel(page), { mode: "illustrative", ...modeLine(trace.meta) });
    } finally {
      await page.close();
    }
  });

  it("shows the REPLAY label with its date range and source on screen", async () => {
    const page = await openEmbed({ scene, aspect, mode: "replay", autoplay: false });
    try {
      const meta = embedReplayMeta(trace.meta.title, trace.events);
      const expected = modeLine(meta);
      expect(expected.detail).toContain(meta.source.ref);
      expectLabelOnScreen(await readLabel(page), { mode: "replay", ...expected });
    } finally {
      await page.close();
    }
  });

  it("opens on the poster frame and stays there when not asked to autoplay", async () => {
    const page = await openEmbed({ scene, aspect, autoplay: false });
    try {
      expect(await frameOf(page)).toBe(poster);
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(page)).toBe(poster);
    } finally {
      await page.close();
    }
  });

  it("under prefers-reduced-motion: reduce shows the poster with a play control and never autoplays", async () => {
    // Asked to autoplay and given no controls: the preference overrides both.
    const page = await openEmbed({ scene, aspect, autoplay: true, controls: false, reducedMotion: true });
    try {
      expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
      expect(await frameOf(page)).toBe(poster);
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(page), "a reduced-motion embed never autoplays").toBe(poster);
      expectLabelOnScreen(await readLabel(page), { mode: "illustrative", ...modeLine(trace.meta) });
      const play = await page.$('button[aria-label="Play video"]');
      expect(play, "a reduced-motion embed shows a play control").not.toBeNull();
      await play?.click();
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(page), "pressing play plays").not.toBe(poster);
    } finally {
      await page.close();
    }
  });
});
