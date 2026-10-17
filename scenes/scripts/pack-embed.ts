/**
 * Build the embeddable package: `pnpm embed:pack [--out <dir>]`.
 *
 * Output: `<out>/qontinui-studio-scenes-<version>.tgz` (default `out/embed/`),
 * an npm tarball a website installs as a file dependency, plus its SHA-256 on
 * stdout. It contains:
 *
 *   dist/player.js     src/adapters/remotion/embed.ts bundled into one ESM
 *                      module; react, react-dom and remotion/@remotion/player
 *                      stay EXTERNAL (peer dependencies), so the consumer
 *                      supplies one copy and loads this as its own lazy chunk
 *   dist/player.d.ts   embed/player-api.ts (held to the source by a unit test)
 *   THIRD_PARTY_LICENSES  the licences of code bundled into dist/player.js (zod)
 *   fonts/             the bundled Inter woff2 files + OFL.txt: serve this
 *                      directory's parent as the player's `assetBaseUrl`
 *   posters/<scene>-16x9.png   each scene's poster frame of its illustrative
 *                      trace at 1920x1080, rendered by the Remotion adapter, so
 *                      a page can show the poster before the player loads
 *   manifest.json      per scene: title, poster frame, duration, fps, poster file
 *   LICENSE, README.md
 *
 * Reproducible for a given toolchain (Node + its bundled npm; CI pins the
 * exact Node version): nothing in the tarball depends on the time.
 * The bundle and npm's tar are deterministic, and the posters come from the
 * same scale-1 renderer the golden frames hold byte-identical (CI is the
 * authority). Every poster must be a golden frame: the pack fails if a poster
 * has no golden or differs from it. So a given studio commit packs to one SHA-256, which a
 * consumer records next to the vendored tarball and anyone can re-derive.
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

import { build } from "esbuild";

import { RemotionAdapter } from "../src/adapters/remotion/render";
import { ASPECT_SIZES, STUDIO_FPS } from "../src/api/layout";
import { posterFrame, sceneDuration } from "../src/api/scene-root";
import { parseTrace } from "../src/api/trace/schema";
import { scenes } from "../src/scenes";
import { illustrativeTraces } from "../src/scenes/illustrative-traces";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pkg = JSON.parse(readFileSync(path.join(ROOT, "package.json"), "utf8")) as {
  version: string;
  peerDependencies: Record<string, string>;
  dependencies: Record<string, string>;
};

/** Modules the consumer supplies. Everything else (zod, the studio) is bundled. */
const EXTERNAL = ["react", "react/*", "react-dom", "react-dom/*", "remotion", "remotion/*", "@remotion/player"];

const POSTER_ASPECT = "16:9" as const;

const { values } = parseArgs({ options: { out: { type: "string" } } });
const outDir = path.resolve(values.out ?? path.join(ROOT, "out/embed"));
const stage = path.join(outDir, "package");
rmSync(stage, { recursive: true, force: true });
mkdirSync(path.join(stage, "dist"), { recursive: true });
mkdirSync(path.join(stage, "fonts"), { recursive: true });
mkdirSync(path.join(stage, "posters"), { recursive: true });

// 1. The player bundle.
const bundle = await build({
  absWorkingDir: ROOT,
  entryPoints: ["src/adapters/remotion/embed.ts"],
  outfile: path.join(stage, "dist/player.js"),
  bundle: true,
  platform: "browser",
  format: "esm",
  target: "es2022",
  jsx: "automatic",
  external: EXTERNAL,
  // The test-only replay hook is not in this graph, but pin its switch off
  // anyway: the package must never attest a replay.
  define: { "process.env.VITEST": '""' },
  legalComments: "inline",
  logLevel: "warning",
  metafile: true,
});
const inputs = Object.keys(bundle.metafile.inputs);
const leaked = inputs.filter(
  (file) =>
    /node_modules\/(@remotion\/(?!player)|@hyperframes|hyperframes)/.test(file) ||
    /^src\/(cli|adapters\/hyperframes)\/|^src\/adapters\/remotion\/(render|entry|Root)\./.test(file),
);
if (leaked.length) throw new Error(`the player bundle reached a render-only engine module: ${leaked.join(", ")}`);
copyFileSync(path.join(ROOT, "embed/player-api.ts"), path.join(stage, "dist/player.d.ts"));

// Licences of the third-party code esbuild inlined into the bundle (React and
// Remotion are external, so only these). A new bundled package fails the pack
// until it is listed here.
const BUNDLED_THIRD_PARTY = ["zod"];
const bundledPackages = new Set(
  inputs.flatMap((file) => {
    const match = /node_modules\/(?:\.pnpm\/[^/]+\/node_modules\/)?((?:@[^/]+\/)?[^/]+)\//.exec(file);
    return match?.[1] ? [match[1]] : [];
  }),
);
const unlisted = [...bundledPackages].filter((name) => !BUNDLED_THIRD_PARTY.includes(name));
if (unlisted.length) throw new Error(`bundled packages with no licence entry in pack-embed.ts: ${unlisted.join(", ")}`);
const thirdParty = BUNDLED_THIRD_PARTY.map((name) => {
  const dir = path.join(ROOT, "node_modules", name);
  const { version, license } = JSON.parse(readFileSync(path.join(dir, "package.json"), "utf8")) as { version: string; license: string };
  return `${name}@${version} (${license})\n\n${readFileSync(path.join(dir, "LICENSE"), "utf8").trim()}\n`;
});
writeFileSync(
  path.join(stage, "THIRD_PARTY_LICENSES"),
  `Third-party code bundled into dist/player.js\n\n${thirdParty.join("\n---\n\n")}`,
);

// 2. Fonts.
for (const file of readdirSync(path.join(ROOT, "assets/fonts")).sort()) {
  copyFileSync(path.join(ROOT, "assets/fonts", file), path.join(stage, "fonts", file));
}

// 3. Posters, and the manifest that describes them.
const manifest: Record<string, unknown> = {};
const serveUrl = await RemotionAdapter.bundle();
const adapter = new RemotionAdapter({ serveUrl });
try {
  for (const scene of Object.values(scenes)) {
    const trace = parseTrace(illustrativeTraces[scene.id]);
    const poster = posterFrame(scene, trace);
    const [written] = await adapter.renderStills({
      scene: scene.id,
      trace,
      aspect: POSTER_ASPECT,
      frames: [poster],
      outDir: path.join(outDir, "stills"),
      prefix: `${scene.id}-`,
      scale: 1,
      overwrite: true,
    });
    if (!written) throw new Error(`no poster was written for ${scene.id}`);
    // The goldens are the CI-verified authority for these pixels.
    const golden = path.join(ROOT, "tests/golden/__goldens__", `${scene.id}-16x9-f${String(poster).padStart(4, "0")}.png.sha256`);
    // Every shipped poster must BE a golden frame: unverified pixels never ship.
    if (!existsSync(golden)) {
      throw new Error(`${scene.id}: poster frame ${poster} has no 16:9 golden (${golden}); add it to tests/golden/cases.ts`);
    }
    const want = readFileSync(golden, "utf8").trim();
    const got = createHash("sha256").update(readFileSync(written)).digest("hex");
    if (got !== want) throw new Error(`${scene.id}: poster ${got} differs from its golden frame ${want} (${golden})`);
    const file = `posters/${scene.id}-16x9.png`;
    copyFileSync(written, path.join(stage, file));
    manifest[scene.id] = {
      title: scene.title,
      mode: trace.meta.mode,
      fps: STUDIO_FPS,
      durationInFrames: sceneDuration(scene, trace),
      posterFrame: poster,
      poster: { file, aspect: POSTER_ASPECT, ...ASPECT_SIZES[POSTER_ASPECT] },
    };
  }
} finally {
  await adapter.close();
}
rmSync(path.join(outDir, "stills"), { recursive: true, force: true });
writeFileSync(path.join(stage, "manifest.json"), `${JSON.stringify({ scenes: manifest }, null, 2)}\n`);

// 4. Licence, README, package.json.
copyFileSync(path.join(ROOT, "LICENSE"), path.join(stage, "LICENSE"));
copyFileSync(path.join(ROOT, "embed/README.md"), path.join(stage, "README.md"));
const peer = (name: string) => {
  const version = pkg.peerDependencies[name];
  if (!version) throw new Error(`package.json has no peer dependency ${name}`);
  return version;
};
/** `^<major>.0.0` of the React version the studio is tested against. */
const majorRange = (name: string) => {
  const version = pkg.dependencies[name];
  if (!version) throw new Error(`package.json has no dependency ${name}`);
  return `^${version.split(".")[0]}.0.0`;
};
const manifestPkg = {
  name: "@qontinui/studio-scenes",
  version: pkg.version,
  description: "Embeddable web player for the Qontinui studio's process explainers (built by `pnpm embed:pack`).",
  license: "MIT",
  repository: { type: "git", url: "git+https://github.com/qontinui/qontinui-studio.git", directory: "scenes" },
  type: "module",
  sideEffects: false,
  exports: {
    "./player": { types: "./dist/player.d.ts", import: "./dist/player.js", default: "./dist/player.js" },
    "./manifest.json": "./manifest.json",
    "./posters/*": "./posters/*",
    "./fonts/*": "./fonts/*",
    "./package.json": "./package.json",
  },
  files: ["dist", "fonts", "posters", "manifest.json", "LICENSE", "THIRD_PARTY_LICENSES", "README.md"],
  peerDependencies: {
    react: majorRange("react"),
    "react-dom": majorRange("react-dom"),
    remotion: peer("remotion"),
    "@remotion/player": peer("@remotion/player"),
  },
};
writeFileSync(path.join(stage, "package.json"), `${JSON.stringify(manifestPkg, null, 2)}\n`);

// 5. The tarball.
const packed = execFileSync("npm", ["pack", "--json", "--pack-destination", outDir], { cwd: stage, encoding: "utf8" });
const [entry] = JSON.parse(packed) as { filename: string }[];
if (!entry) throw new Error(`npm pack reported no tarball: ${packed}`);
const tarball = path.join(outDir, entry.filename);
const sha256 = createHash("sha256").update(readFileSync(tarball)).digest("hex");
process.stdout.write(`${tarball}\nsha256 ${sha256}\n`);
