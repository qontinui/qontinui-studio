/**
 * The authority for the D7 engine seam: the RESOLVED module graph.
 *
 * Lint sees one file at a time, so a re-export through a module outside its
 * globs (or any indirection it does not model) can still pull the engine in.
 * This test bundles every file under src/ except src/adapters/** and
 * src/cli/** with esbuild, one entry point each, and fails if any module in a
 * graph is Remotion or an adapter. Nothing is externalised except Node
 * builtins.
 */
import { globSync } from "node:fs";
import { builtinModules } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";
import { describe, expect, it } from "vitest";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

/**
 * Every source file outside the engine's own territory is an entry point, so
 * a module no index re-exports (scene-root.tsx, trace/testing.ts, …) is still
 * checked.
 */
const ENTRY_POINTS = globSync("src/**/*.{ts,tsx}", { cwd: ROOT })
  .filter((file) => !/^src\/(adapters|cli)\//.test(file))
  .sort();
const FORBIDDEN = [/node_modules\/(@remotion|remotion)\//, /(^|\/)src\/adapters\//];

async function graphOf(entry: string): Promise<string[]> {
  const result = await build({
    absWorkingDir: ROOT,
    entryPoints: [entry],
    bundle: true,
    write: false,
    metafile: true,
    platform: "browser",
    format: "esm",
    jsx: "automatic",
    logLevel: "silent",
    external: [...builtinModules, ...builtinModules.map((m) => `node:${m}`)],
  });
  // pnpm paths look like node_modules/.pnpm/remotion@x/node_modules/remotion/…
  return Object.keys(result.metafile.inputs);
}

describe("engine seam: resolved module graph (plan D7)", () => {
  it("covers the files no index re-exports", () => {
    expect(ENTRY_POINTS).toContain("src/api/scene-root.tsx");
    expect(ENTRY_POINTS).toContain("src/api/trace/testing.ts");
    expect(ENTRY_POINTS.some((f) => f.startsWith("src/adapters/") || f.startsWith("src/cli/"))).toBe(false);
  });

  it.each(ENTRY_POINTS)("%s reaches no engine and no adapter", async (entry) => {
    const inputs = await graphOf(entry);
    expect(inputs, "the entry itself is in its graph").toContain(entry);
    const offending = inputs.filter((input) => FORBIDDEN.some((re) => re.test(input)));
    expect(offending, `${entry} pulls in: ${offending.join(", ")}`).toEqual([]);
  });

  it("the check is live: the adapter entry does reach Remotion", async () => {
    const inputs = await graphOf("src/adapters/remotion/StudioComposition.tsx");
    expect(inputs.some((input) => /node_modules\/(@remotion|remotion)\//.test(input))).toBe(true);
  });
});
