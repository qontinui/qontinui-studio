/**
 * Builds a HyperFrames project directory for one studio render:
 *
 *   index.html        the composition root (size, duration) + the render config
 *   studio-scene.js   ./page.tsx bundled with esbuild (React, the scene, the API)
 *   fonts/*.woff2     the bundled Inter, copied from assets/fonts
 *
 * Everything the page needs is in the directory, so the render runs with no
 * network at all (the adapter starts the container with `--network none`).
 */
import { copyFile, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

import { ASPECT_SIZES, STUDIO_FPS, type Aspect } from "../../api/layout";
import { fontFiles } from "../../api/typography";
import { assertRenderable } from "../../api/trace/provenance";
import type { SceneTrace } from "../../api/trace/schema";
import { STUDIO_CONFIG_ELEMENT_ID, type StudioPageConfig } from "./page-config";

export const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const PAGE_ENTRY = path.join(PACKAGE_ROOT, "src/adapters/hyperframes/page.tsx");
const ASSETS_DIR = path.join(PACKAGE_ROOT, "assets");

/** HyperFrames composition id of every studio project (one composition each). */
export const COMPOSITION_ID = "studio";
const MOUNT_ID = "studio-mount";

/** Bundle the page once; the same bytes serve every project. */
let bundled: Promise<string> | null = null;
export function bundlePage(): Promise<string> {
  bundled ??= build({
    entryPoints: [PAGE_ENTRY],
    bundle: true,
    write: false,
    platform: "browser",
    format: "iife",
    target: "es2022",
    jsx: "automatic",
    minify: false,
    legalComments: "none",
    define: { "process.env.NODE_ENV": '"production"' },
    logLevel: "silent",
  }).then((result) => {
    const file = result.outputFiles[0];
    if (!file) throw new Error("esbuild produced no page bundle");
    return file.text;
  });
  return bundled;
}

export interface ProjectSpec {
  sceneId: string;
  trace: SceneTrace;
  aspect: Aspect;
  /** Scene frame for each composition frame. */
  frames: readonly number[];
  /**
   * Composition frame rate (default STUDIO_FPS). A GIF runs at half rate over
   * every second scene frame; the scene's own timing is unchanged.
   */
  fps?: number;
}

/** JSON safe to embed in a <script> element. */
function scriptJson(value: unknown): string {
  return JSON.stringify(value).replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/&/g, "\\u0026");
}

/**
 * Writes a playable composition, so it is a render path: a replay without
 * build-record provenance is refused here too (plan D2), not only by the
 * adapter that calls it.
 */
export async function writeProject(dir: string, spec: ProjectSpec): Promise<void> {
  assertRenderable(spec.trace);
  const { width, height } = ASPECT_SIZES[spec.aspect];
  const fps = spec.fps ?? STUDIO_FPS;
  const config: StudioPageConfig = {
    compositionId: COMPOSITION_ID,
    mountId: MOUNT_ID,
    sceneId: spec.sceneId,
    aspect: spec.aspect,
    width,
    height,
    fps,
    frames: [...spec.frames],
    trace: spec.trace,
  };
  // Seconds, as HyperFrames declares duration; frames = duration * fps.
  const durationSeconds = spec.frames.length / fps;
  const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=${width}, height=${height}" />
    <style>
      html, body { margin: 0; padding: 0; background: #000; }
      #root { position: relative; width: ${width}px; height: ${height}px; overflow: hidden; }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="${COMPOSITION_ID}" data-start="0" data-duration="${durationSeconds}" data-width="${width}" data-height="${height}">
      <div id="${MOUNT_ID}"></div>
    </div>
    <script type="application/json" id="${STUDIO_CONFIG_ELEMENT_ID}">${scriptJson(config)}</script>
    <script src="studio-scene.js"></script>
  </body>
</html>
`;
  await mkdir(path.join(dir, "fonts"), { recursive: true });
  await writeFile(path.join(dir, "index.html"), html);
  await writeFile(path.join(dir, "studio-scene.js"), await bundlePage());
  for (const { file } of fontFiles) await copyFile(path.join(ASSETS_DIR, file), path.join(dir, file));
}
