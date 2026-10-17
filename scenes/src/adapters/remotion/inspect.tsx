/**
 * Text layout inspection for the publish gate (plan Phase 5: "no text
 * overflow or overlap"). Each sampled program frame is server-rendered to
 * static markup — the same `ProgramRoot` every render draws — and laid out
 * in the same pinned Chrome Headless Shell the renders use, under the
 * studio's fontconfig and fonts, so the boxes measured are the boxes
 * rendered. In the page, every visible text run is checked:
 *
 * - **outside-frame:** visible text reaches outside the output frame;
 * - **clipped:** a box that clips cuts text off with no ellipsis;
 * - **truncated:** text is shortened with an ellipsis by its own layout
 *   (`text-overflow: ellipsis`, `-webkit-line-clamp`) — designed, and
 *   reported for the review page rather than failing the gate;
 * - **occluded:** visible text is partly covered by an opaque box;
 * - **overlap:** two visible text runs are drawn over each other.
 *
 * Text that is (effectively) transparent or hidden is not checked. This
 * module is engine-specific only in where the browser binary comes from
 * (Remotion's pinned Chrome Headless Shell).
 */
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ensureBrowser } from "@remotion/renderer";
import puppeteer from "puppeteer-core";
import { renderToStaticMarkup } from "react-dom/server";

import type { Program } from "../../api/composition";
import { ASPECT_SIZES, type Aspect } from "../../api/layout";
import { ProgramRoot } from "../../api/program-root";
import { FONT_FAMILY, fontFiles } from "../../api/typography";
import { scenes } from "../../scenes";

const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const FONTCONFIG_FILE = path.join(PACKAGE_ROOT, "fontconfig/fonts.conf");

export type TextProblemKind = "outside-frame" | "clipped" | "truncated" | "occluded" | "overlap";

export interface TextProblem {
  frame: number;
  segment: string;
  kind: TextProblemKind;
  text: string;
  /** For an overlap: the other text run. */
  other?: string;
  detail: string;
}

export interface LayoutInspection {
  frames: number[];
  textRuns: number;
  problems: TextProblem[];
  /** Frames in which no visible text was found at all. */
  emptyFrames: number[];
}

/** Line boxes smaller than this (px) in either direction are ignored, and overlaps must exceed it. */
const TOLERANCE_PX = 1;

/**
 * Runs in the page: every problem with the visible text in
 * `[data-studio-aspect]`.
 *
 * Measuring rules:
 * - each text run's line boxes are cut, in turn, by every ancestor that
 *   clips (`overflow` other than visible), up to and including the frame
 *   root; what remains is what the viewer sees;
 * - a box cuts a line horizontally when the line extends past it by more
 *   than the tolerance, and vertically when more than a fifth of the line's
 *   height lies outside it (a tight line-height makes every glyph box poke
 *   out a little, which is not a cut);
 * - a cut by the frame root is `outside-frame`; a cut by a box that
 *   truncates its OWN content by design — a horizontal cut by a box with
 *   `text-overflow: ellipsis`, a vertical one by a box with
 *   `-webkit-line-clamp` — is `truncated` (the viewer sees an ellipsis); any
 *   other cut is `clipped`. Every clipping box is checked, so a label
 *   ellipsized by its own box and also cut by a card or the frame reports
 *   both;
 * - visible text is sampled at five points along every line: text partly
 *   under an opaque box that is not its ancestor is `occluded` (wholly
 *   covered text is invisible, and skipped);
 * - two runs `overlap` when the middle halves of their visible line boxes
 *   intersect and, at some point of the intersection, neither is covered.
 */
function checkText(tolerance: number) {
  type Box = { left: number; top: number; right: number; bottom: number };
  const out: { kind: string; text: string; other?: string; detail: string }[] = [];
  const root = document.querySelector("[data-studio-aspect]");
  if (!root) return { runs: 0, problems: [{ kind: "outside-frame", text: "", detail: "no frame root in the page" }] };
  const frame = root.getBoundingClientRect();
  const short = (s: string) => (s.length > 60 ? `${s.slice(0, 57)}...` : s);
  const opacityOf = (el: Element | null): number => {
    let o = 1;
    for (let e = el; e; e = e.parentElement) {
      const style = getComputedStyle(e);
      if (style.display === "none" || style.visibility === "hidden") return 0;
      o *= Number(style.opacity);
    }
    return o;
  };
  const clips = (style: CSSStyleDeclaration) => style.overflowX !== "visible" || style.overflowY !== "visible";
  const clamps = (style: CSSStyleDeclaration) => {
    const clamp = style.getPropertyValue("-webkit-line-clamp");
    return clamp !== "" && clamp !== "none";
  };
  const mid = (b: Box) => (b.top + b.bottom) / 2;
  const overhang = (b: Box, r: Box) => Math.max(0, r.top - b.top, b.bottom - r.bottom);
  const opaque = (el: Element) => {
    if (el.tagName === "IMG") return true;
    const m = /rgba?\(([^)]*)\)/.exec(getComputedStyle(el).backgroundColor);
    if (!m) return false;
    const parts = (m[1] as string).split(",").map((x) => Number(x.trim()));
    return parts.length < 4 || (parts[3] as number) >= 0.95;
  };
  /** Whether text of `el` at (x, y) lies under an opaque box that is not its own ancestor. */
  const hiddenAt = (el: Element, x: number, y: number) => {
    for (const e of document.elementsFromPoint(x, y)) {
      if (e === el || e.contains(el)) return false;
      if (!el.contains(e) && opaque(e) && opacityOf(e) >= 0.95) return true;
    }
    return false;
  };
  /** Points along a line box: five across, at a quarter, half and three quarters of its height. */
  const along = (b: Box) =>
    [0.25, 0.5, 0.75].flatMap((v) =>
      [0.1, 0.3, 0.5, 0.7, 0.9].map((f) => [b.left + (b.right - b.left) * f, b.top + (b.bottom - b.top) * v] as const),
    );

  const runs: { text: string; boxes: Box[]; el: Element }[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = (node.textContent ?? "").trim();
    const el = node.parentElement;
    if (!text || !el || opacityOf(el) < 0.05) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    let visible: Box[] = [...range.getClientRects()]
      .filter((r) => r.width > tolerance && r.height > tolerance)
      .map((r) => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom }));
    if (!visible.length) continue;
    const kinds = new Map<string, string>();
    for (let a: Element | null = el; a && a !== root.parentElement; a = a.parentElement) {
      const style = getComputedStyle(a);
      if (!clips(style)) continue;
      const r = a.getBoundingClientRect();
      const horizontal = visible.some((b) => b.left < r.left - tolerance || b.right > r.right + tolerance);
      const vertical = visible.some((b) => overhang(b, r) > Math.max(tolerance, (b.bottom - b.top) / 5));
      if (horizontal || vertical) {
        const designed =
          (!horizontal || style.textOverflow === "ellipsis") && (!vertical || clamps(style));
        const kind = a === root ? "outside-frame" : designed ? "truncated" : "clipped";
        if (!kinds.has(kind)) {
          kinds.set(
            kind,
            kind === "outside-frame"
              ? `runs off the ${Math.round(frame.width)}x${Math.round(frame.height)} frame`
              : kind === "truncated"
                ? "shortened with an ellipsis by its own box"
                : `cut off by a clipping box ${Math.round(r.width)}x${Math.round(r.height)} with no ellipsis`,
          );
        }
      }
      visible = visible
        .filter((b) => mid(b) >= r.top && mid(b) <= r.bottom)
        .map((b) => ({ left: Math.max(b.left, r.left), top: Math.max(b.top, r.top), right: Math.min(b.right, r.right), bottom: Math.min(b.bottom, r.bottom) }))
        .filter((b) => b.right - b.left > tolerance);
    }
    for (const [kind, detail] of kinds) out.push({ kind, text: short(text), detail });
    if (!visible.length) continue;
    const samples = visible.flatMap(along).map(([x, y]) => hiddenAt(el, x, y));
    if (samples.every(Boolean)) continue;
    if (samples.some(Boolean)) out.push({ kind: "occluded", text: short(text), detail: "partly covered by an opaque box" });
    runs.push({ text, boxes: visible, el });
  }

  const core = (b: Box): Box => {
    const h = (b.bottom - b.top) / 4;
    return { left: b.left + tolerance, right: b.right - tolerance, top: b.top + h, bottom: b.bottom - h };
  };
  for (let i = 0; i < runs.length; i++) {
    for (let j = i + 1; j < runs.length; j++) {
      const a = runs[i]!;
      const b = runs[j]!;
      let seen = false;
      for (const x of a.boxes.map(core)) {
        for (const y of b.boxes.map(core)) {
          const box = { left: Math.max(x.left, y.left), right: Math.min(x.right, y.right), top: Math.max(x.top, y.top), bottom: Math.min(x.bottom, y.bottom) };
          if (box.right - box.left <= tolerance || box.bottom - box.top <= tolerance) continue;
          if (along(box).some(([px, py]) => !hiddenAt(a.el, px, py) && !hiddenAt(b.el, px, py))) seen = true;
        }
      }
      if (seen) out.push({ kind: "overlap", text: short(a.text), other: short(b.text), detail: "their visible line boxes intersect" });
    }
  }
  return { runs: runs.length, problems: out };
}

/** The bundled fonts, as base64, for `FontFace` in the page (nothing is fetched). */
async function fontSources(): Promise<{ weight: number; base64: string }[]> {
  const out: { weight: number; base64: string }[] = [];
  for (const { weight, file } of fontFiles) {
    out.push({ weight, base64: (await readFile(path.join(PACKAGE_ROOT, "assets", file))).toString("base64") });
  }
  return out;
}

const MIME: Record<string, string> = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp" };

/**
 * Inspect `frames` of `program`. `images` maps each b-roll `src` to a local
 * file (its pixels do not matter for text, but the badge sits over it).
 *
 * One page is used for every frame: the fonts are registered once from
 * their bytes (`new FontFace(family, buffer)`; a font fetched by URL loads
 * lazily, and a page with no font measures nothing real — the studio
 * fontconfig offers no fallback), then each frame's static markup replaces
 * the page body.
 */
export async function inspectProgramLayout(
  program: Program,
  aspect: Aspect,
  frames: readonly number[],
  images: Readonly<Record<string, string>> = {},
): Promise<LayoutInspection> {
  const dataUrls: Record<string, string> = {};
  for (const [src, file] of Object.entries(images)) {
    dataUrls[src] = `data:${MIME[path.extname(file).toLowerCase()] ?? "application/octet-stream"};base64,${(await readFile(file)).toString("base64")}`;
  }
  const Image = ({ src, style }: { src: string; style: Record<string, string | number> }) => (
    <img src={dataUrls[src] ?? ""} style={style} alt="" />
  );
  return inspectMarkup(
    frames.map((frame) => ({
      frame,
      html: renderToStaticMarkup(<ProgramRoot program={program} frame={frame} aspect={aspect} scenes={scenes} Image={Image} />),
    })),
    aspect,
  );
}

/**
 * Lay out each page body (`html` must contain one `[data-studio-aspect]`
 * frame root) in the pinned browser with the studio fonts, and check its
 * text. Exported for the checker's own tests.
 */
export async function inspectMarkup(pages: readonly { frame: number; html: string }[], aspect: Aspect): Promise<LayoutInspection> {
  const status = await ensureBrowser({ logLevel: "error" });
  if (!("path" in status)) throw new Error(`no usable browser: ${JSON.stringify(status)}`);
  const { width, height } = ASPECT_SIZES[aspect];
  const fonts = await fontSources();
  const browser = await puppeteer.launch({
    executablePath: status.path,
    headless: "shell",
    // Nothing is loaded from the network or disk; a CI runner's AppArmor refuses the sandbox.
    args: ["--no-sandbox"],
    env: { ...process.env, FONTCONFIG_FILE },
  });
  const problems: TextProblem[] = [];
  const emptyFrames: number[] = [];
  let textRuns = 0;
  try {
    const page = await browser.newPage();
    await page.setViewport({ width, height });
    // A TypeScript transform (tsx/esbuild keepNames) can wrap the named
    // functions inside checkText in __name(); the page has no such helper.
    await page.evaluateOnNewDocument("globalThis.__name = (fn) => fn;");
    await page.setContent(
      `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;width:${width}px;height:${height}px;overflow:hidden}*{pointer-events:auto!important}</style></head><body></body></html>`,
    );
    await page.evaluate("globalThis.__name = (fn) => fn;");
    const failed = await page.evaluate(
      async (family: string, sources: { weight: number; base64: string }[]) => {
        const out: string[] = [];
        for (const { weight, base64 } of sources) {
          const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
          const face = new FontFace(family, bytes.buffer, { weight: String(weight), style: "normal" });
          try {
            await face.load();
            document.fonts.add(face);
          } catch (err) {
            out.push(`${weight}: ${String(err)}`);
          }
        }
        return out;
      },
      FONT_FAMILY,
      fonts,
    );
    if (failed.length) throw new Error(`the studio fonts did not load for layout inspection: ${failed.join("; ")}`);
    for (const { frame, html: markup } of pages) {
      await page.evaluate(async (html: string) => {
        document.body.innerHTML = html;
        await Promise.all([...document.images].map((img) => (img.complete ? null : img.decode().catch(() => null))));
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      }, markup);
      const segment = (await page.evaluate(() => document.querySelector("[data-studio-segment]")?.getAttribute("data-studio-segment") ?? "")) || "?";
      const result = await page.evaluate(checkText, TOLERANCE_PX);
      textRuns += result.runs;
      if (!result.runs) emptyFrames.push(frame);
      for (const p of result.problems) problems.push({ frame, segment, ...p, kind: p.kind as TextProblemKind });
    }
  } finally {
    await browser.close().catch(() => undefined);
  }
  return { frames: pages.map((p) => p.frame), textRuns, problems, emptyFrames };
}
