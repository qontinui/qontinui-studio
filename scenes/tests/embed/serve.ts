/**
 * Build and serve the embed test page (`./page.tsx`) on 127.0.0.1.
 *
 * Routes: `/` the page, `/page.js` its bundle, `/assets/fonts/*` the bundled
 * fonts (the player's `assetBaseUrl`), `/traces/<scene>.json` the committed
 * illustrative traces. Nothing else is served, and nothing outside those
 * two directories can be reached (no `..`, no other files).
 */
import { readFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const FONTS_DIR = path.join(ROOT, "assets/fonts");
const TRACES_DIR = path.join(ROOT, "traces/illustrative");

const HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Studio scene embed</title>
<style>body { margin: 24px; background: #0b0b0e; color: #ddd; font-family: sans-serif; }</style>
</head>
<body>
<div id="root"></div>
<script type="module" src="/page.js"></script>
</body>
</html>
`;

/**
 * The page bundle. `process.env.VITEST` is passed through from the process
 * that builds it, so the test-only replay hook works under vitest (the scene
 * matrix) and refuses everywhere else (`pnpm embed:serve`).
 */
export async function bundleEmbedPage(): Promise<string> {
  const result = await build({
    absWorkingDir: ROOT,
    entryPoints: ["tests/embed/page.tsx"],
    bundle: true,
    write: false,
    platform: "browser",
    format: "esm",
    jsx: "automatic",
    logLevel: "silent",
    define: {
      "process.env.NODE_ENV": '"production"',
      "process.env.VITEST": JSON.stringify(process.env.VITEST ?? ""),
    },
  });
  const [output] = result.outputFiles;
  if (!output) throw new Error("esbuild produced no output for the embed page");
  return output.text;
}

const FILE_NAME = /^[A-Za-z0-9._-]+$/;

async function serveFile(dir: string, name: string, type: string) {
  if (!FILE_NAME.test(name) || name.startsWith(".")) return null;
  try {
    return { type, body: await readFile(path.join(dir, name)) };
  } catch {
    return null;
  }
}

export interface EmbedServer {
  /** Base URL, e.g. http://127.0.0.1:43210 */
  url: string;
  close(): Promise<void>;
}

export async function serveEmbedPage(port = 0): Promise<EmbedServer> {
  const script = await bundleEmbedPage();
  const server: Server = createServer((req, res) => {
    const pathname = new URL(req.url ?? "/", "http://localhost").pathname;
    const respond = async () => {
      if (pathname === "/") return { type: "text/html; charset=utf-8", body: HTML };
      if (pathname === "/page.js") return { type: "text/javascript; charset=utf-8", body: script };
      const font = /^\/assets\/fonts\/([^/]+\.woff2)$/.exec(pathname);
      if (font) return serveFile(FONTS_DIR, font[1] as string, "font/woff2");
      const trace = /^\/traces\/([^/]+\.json)$/.exec(pathname);
      if (trace) return serveFile(TRACES_DIR, decodeURIComponent(trace[1] as string), "application/json");
      return null;
    };
    respond().then(
      (found) => {
        if (!found) {
          res.writeHead(404).end();
          return;
        }
        res.writeHead(200, { "content-type": found.type, "cache-control": "no-store" }).end(found.body);
      },
      () => res.writeHead(500).end(),
    );
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => resolve());
  });
  const address = server.address();
  const bound = typeof address === "object" && address ? address.port : port;
  return {
    url: `http://127.0.0.1:${bound}`,
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}
