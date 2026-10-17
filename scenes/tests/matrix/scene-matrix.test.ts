/**
 * The Phase 3 scene matrix (plan arming line: "per-scene render matrix (16:9,
 * 9:16, 1:1) passes and asserts the on-screen D2 mode label").
 *
 * For every registered scene, at each aspect in `SCENE_MATRIX_ASPECTS`
 * (comma-separated; default all three — CI runs one job per aspect):
 *
 * - **Render:** the whole scene renders to mp4 through the engine adapter;
 *   the encoded file is read back (size, fps, codec, length = the timeline's
 *   duration), and its poster frame renders as a still of the native size.
 * - **Embed:** the adapter's web player mounts the scene in the embed test
 *   page (`tests/embed/`), from the scene's committed illustrative trace, and
 *   plays when asked to autoplay.
 * - **D2 label, on screen:** in the embed, the mode label shows the trace's
 *   own mode line (`modeLine`): ILLUSTRATIVE for the illustrative trace;
 *   REPLAY plus date range and source for the same events as an attested
 *   replay. "On screen" means, for the badge and the detail line each:
 *   inside the scene's frame, non-empty, opaque, visible, text colour opaque
 *   and contrasting with its background; the label is the topmost scene
 *   element at its centre; and screenshots of it with and without its text differ.
 * - **Reduced motion:** under `prefers-reduced-motion: reduce` the embed,
 *   even when asked to autoplay and given no controls, stays on the scene's
 *   poster frame, shows a visible play control, and plays only once it is
 *   pressed. Turning the preference on mid-play sends the embed back to the
 *   poster; turning it off lets an autoplaying embed play again. A
 *   server-rendered, hydrated embed autoplays only once the client reports
 *   no preference. Without the preference, an embed not asked to autoplay
 *   opens on the poster frame and stays there.
 */
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { ensureBrowser, getVideoMetadata } from "@remotion/renderer";
import { PNG } from "pngjs";
import puppeteer, { type Browser, type Page } from "puppeteer-core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { RemotionAdapter } from "../../src/adapters/remotion/render";
import { ASPECTS, ASPECT_SIZES, STUDIO_FPS, isAspect, type Aspect } from "../../src/api/layout";
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
  /** Server-render the player, then hydrate it. */
  ssr?: boolean;
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
    ssr: options.ssr ? "1" : "0",
  });
  await page.goto(`${server.url}/?${query.toString()}`);
  await page.waitForFunction(
    () =>
      document.body.dataset.embedError !== undefined ||
      document.querySelector("[data-studio-scene]") !== null ||
      document.querySelector("[data-studio-player-error]") !== null,
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

interface PartReading {
  text: string;
  inside: boolean;
  size: { width: number; height: number };
  /** Visible, displayed and fully opaque, up to the scene root. */
  visible: boolean;
  /** The text colour is opaque and differs from the background behind it. */
  legible: boolean;
}

interface LabelReading {
  mode: string | null;
  badge: PartReading;
  detail: PartReading;
  /** Nothing in the scene sits above the label at its centre. */
  topmost: boolean;
  rect: { x: number; y: number; width: number; height: number };
}

/** What the mode label shows, and whether each part of it is really on screen. */
function readLabel(page: Page): Promise<LabelReading> {
  return page.evaluate(() => {
    const root = document.querySelector("[data-studio-scene]") as HTMLElement;
    const label = root.querySelector("[data-studio-mode]") as HTMLElement | null;
    if (!label) throw new Error("no [data-studio-mode] label in the scene");
    const r = root.getBoundingClientRect();
    const rgba = (css: string): number[] | null => {
      const m = /^rgba?\(([^)]+)\)$/.exec(css.trim());
      if (!m) return null;
      const parts = (m[1] as string).split(/[\s,/]+/).filter(Boolean).map(Number);
      return parts.length === 3 ? [...parts, 1] : parts.length === 4 ? parts : null;
    };
    /** The first opaque-ish background at or above `el`. */
    const backgroundOf = (el: HTMLElement): number[] | null => {
      for (let at: HTMLElement | null = el; at; at = at.parentElement) {
        const bg = rgba(getComputedStyle(at).backgroundColor);
        if (bg && (bg[3] as number) > 0) return bg;
      }
      return null;
    };
    const part = (el: HTMLElement | null): PartReading => {
      if (!el) return { text: "", inside: false, size: { width: 0, height: 0 }, visible: false, legible: false };
      const b = el.getBoundingClientRect();
      let visible = true;
      for (let at: HTMLElement | null = el; at && at !== root.parentElement; at = at.parentElement) {
        const style = getComputedStyle(at);
        if (style.visibility !== "visible" || style.display === "none" || Number(style.opacity) < 1) visible = false;
      }
      const color = rgba(getComputedStyle(el).color);
      const bg = backgroundOf(el);
      const legible =
        color !== null && color[3] === 1 && bg !== null && color.slice(0, 3).some((c, i) => Math.abs(c - (bg[i] as number)) > 32);
      return {
        text: (el.textContent ?? "").trim(),
        inside: b.left >= r.left - 0.5 && b.top >= r.top - 0.5 && b.right <= r.right + 0.5 && b.bottom <= r.bottom + 0.5,
        size: { width: b.width, height: b.height },
        visible,
        legible,
      };
    };
    // The label layer takes no pointer events (it must not block the
    // player's controls), so hit-testing needs it switched on for a moment.
    const l = label.getBoundingClientRect();
    const layer = label.parentElement as HTMLElement;
    const previous = layer.style.pointerEvents;
    layer.style.pointerEvents = "auto";
    const hits = document.elementsFromPoint((l.left + l.right) / 2, (l.top + l.bottom) / 2);
    layer.style.pointerEvents = previous;
    const topInScene = hits.find((el) => root.contains(el));
    return {
      mode: label.dataset.studioMode ?? null,
      badge: part(label.firstElementChild as HTMLElement | null),
      detail: part(label.querySelector("[data-studio-mode-detail]") as HTMLElement | null),
      topmost: topInScene !== undefined && label.contains(topInScene),
      rect: { x: l.left, y: l.top, width: l.width, height: l.height },
    };
  });
}

/**
 * Pixels of the label that are text: the label is screenshotted as painted,
 * then again with its text made invisible, and the differing pixels counted.
 * Borders and badge backgrounds are in both shots, so only drawn glyphs count.
 */
async function labelTextPixels(page: Page, rect: LabelReading["rect"]): Promise<number> {
  const shoot = async () => PNG.sync.read(Buffer.from(await page.screenshot({ clip: rect, type: "png" })));
  const painted = await shoot();
  await page.evaluate(() => {
    const label = document.querySelector("[data-studio-scene] [data-studio-mode]") as HTMLElement;
    const style = document.createElement("style");
    style.id = "matrix-hide-label-text";
    style.textContent = "[data-studio-mode] * { color: transparent !important; -webkit-text-fill-color: transparent !important; }";
    label.ownerDocument.head.appendChild(style);
  });
  const hidden = await shoot();
  await page.evaluate(() => document.getElementById("matrix-hide-label-text")?.remove());
  let differing = 0;
  for (let i = 0; i < painted.data.length; i += 4) {
    if (
      painted.data[i] !== hidden.data[i] ||
      painted.data[i + 1] !== hidden.data[i + 1] ||
      painted.data[i + 2] !== hidden.data[i + 2]
    ) {
      differing++;
    }
  }
  return differing;
}

async function expectLabelOnScreen(page: Page, expected: { mode: string; badge: string; detail: string }) {
  const reading = await readLabel(page);
  expect(reading.mode).toBe(expected.mode);
  expect(reading.badge.text).toBe(expected.badge);
  expect(reading.detail.text).toBe(expected.detail);
  for (const [name, part] of [["badge", reading.badge], ["detail", reading.detail]] as const) {
    expect(part.inside, `the label's ${name} lies inside the scene frame`).toBe(true);
    expect(part.size.width, `the label's ${name} has a size`).toBeGreaterThan(0);
    expect(part.size.height, `the label's ${name} has a size`).toBeGreaterThan(0);
    expect(part.visible, `the label's ${name} is visible and opaque`).toBe(true);
    expect(part.legible, `the label's ${name} text contrasts with its background`).toBe(true);
  }
  expect(reading.topmost, "nothing in the scene covers the mode label").toBe(true);
  // Belt and braces: what the browser actually painted there includes text.
  expect(await labelTextPixels(page, reading.rect), "the label's pixels carry drawn text").toBeGreaterThan(200);
}

/** The play control is there and visible (Remotion hides controls by opacity). */
async function visiblePlayControl(page: Page) {
  const handle = await page.$('button[aria-label="Play video"]');
  expect(handle, "a play control is present").not.toBeNull();
  const shown = await handle!.evaluate((el) => {
    const b = el.getBoundingClientRect();
    let visible = b.width > 0 && b.height > 0;
    for (let at: Element | null = el; at; at = at.parentElement) {
      const style = getComputedStyle(at);
      if (style.visibility !== "visible" || style.display === "none" || Number(style.opacity) === 0) visible = false;
    }
    return visible;
  });
  expect(shown, "the play control is visible").toBe(true);
  return handle!;
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
      // What was actually encoded, read back from the file.
      const video = await getVideoMetadata(result.files[0] as string, { logLevel: "error" });
      expect({ width: video.width, height: video.height, fps: video.fps, codec: video.codec }).toEqual({
        ...ASPECT_SIZES[aspect],
        fps: STUDIO_FPS,
        codec: "h264",
      });
      expect(Math.abs((video.durationInSeconds ?? 0) * STUDIO_FPS - duration), "encoded length").toBeLessThanOrEqual(1);
      const [still] = await adapter.renderStills({ scene, trace, aspect, frames: [poster], outDir: dir, prefix: "poster-" });
      const png = PNG.sync.read(readFileSync(still as string));
      expect({ width: png.width, height: png.height }).toEqual(ASPECT_SIZES[aspect]);
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
      await expectLabelOnScreen(page, { mode: "illustrative", ...modeLine(trace.meta) });
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
      await expectLabelOnScreen(page, { mode: "replay", ...expected });
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
      await expectLabelOnScreen(page, { mode: "illustrative", ...modeLine(trace.meta) });
      const play = await visiblePlayControl(page);
      await play.click();
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(page), "pressing play plays").not.toBe(poster);
    } finally {
      await page.close();
    }
  });

  it("follows a change of preference: reduce sends a playing embed to its poster; lifting it plays again", async () => {
    const page = await openEmbed({ scene, aspect, autoplay: true });
    try {
      const first = await frameOf(page);
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(page), "it autoplays without the preference").not.toBe(first);
      await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
      await page.waitForFunction(
        (p) => Number((document.querySelector("[data-studio-scene]") as HTMLElement | null)?.dataset.studioFrame) === p,
        { timeout: LOAD_TIMEOUT_MS },
        poster,
      );
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(page), "after reduce it stays on the poster").toBe(poster);
      await visiblePlayControl(page);
      await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);
      await wait(PLAY_WAIT_MS);
      const resumed = await frameOf(page);
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(page), "with the preference lifted it autoplays again").not.toBe(resumed);
    } finally {
      await page.close();
    }
  });

  it("server-rendered then hydrated: plays only once the client knows motion is fine", async () => {
    const moving = await openEmbed({ scene, aspect, autoplay: true, ssr: true });
    try {
      const first = await frameOf(moving);
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(moving), "hydrated without the preference, it autoplays").not.toBe(first);
    } finally {
      await moving.close();
    }
    const still = await openEmbed({ scene, aspect, autoplay: true, ssr: true, reducedMotion: true });
    try {
      expect(await frameOf(still)).toBe(poster);
      await wait(PLAY_WAIT_MS);
      expect(await frameOf(still), "hydrated under reduce, it never autoplays").toBe(poster);
      await visiblePlayControl(still);
    } finally {
      await still.close();
    }
  });
});
