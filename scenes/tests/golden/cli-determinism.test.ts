/**
 * The real render path is reproducible: two runs of the CLI, which renders
 * with Remotion's renderFrames/renderMedia (parallel tabs, frames in whatever
 * order the tabs take them), must produce byte-identical output.
 *
 * - `render --format png` (whole sequence, 426 frames, 16:9, scale 1) twice:
 *   every frame's SHA-256 must match.
 * - `render --format mp4` (16:9, scale 1) twice: the files' SHA-256 must match.
 *
 * Only 16:9 is exercised to keep CI time bounded; the order-independence and
 * golden tests cover 9:16 and 1:1 frame by frame.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, describe, expect, it } from "vitest";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const OUT = path.join(ROOT, ".golden-out", "cli");
const TRACE = path.join(ROOT, "traces/illustrative/s1-plan-lifecycle.json");

/** Async (not spawnSync): a blocked event loop starves vitest's worker RPC. */
function render(format: "png" | "mp4", out: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [path.join(ROOT, "bin/studio-scenes.mjs"), "render", "s1-plan-lifecycle", "--trace", TRACE, "--format", format, "--aspect", "16:9", "--out", out],
      { cwd: ROOT, stdio: ["ignore", "ignore", "pipe"] },
    );
    let stderr = "";
    child.stderr.on("data", (chunk: Buffer) => (stderr += chunk.toString()));
    child.on("error", reject);
    child.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`render ${format} failed (${code}):\n${stderr}`))));
  });
}

const sha256 = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");
const hashes = (dir: string) =>
  new Map(
    readdirSync(dir)
      .filter((f) => f.endsWith(".png"))
      .sort()
      .map((f) => [f, sha256(path.join(dir, f))]),
  );

beforeAll(async () => {
  rmSync(OUT, { recursive: true, force: true });
  // Sequential: concurrent renders would share nothing anyway, and this keeps
  // CI memory bounded.
  await render("png", path.join(OUT, "seq-1"));
  await render("png", path.join(OUT, "seq-2"));
  await render("mp4", path.join(OUT, "s1-1.mp4"));
  await render("mp4", path.join(OUT, "s1-2.mp4"));
}, 3_600_000);

describe("CLI render path is reproducible (16:9, scale 1)", () => {
  it("two png sequence runs are byte-identical, frame by frame", () => {
    const a = hashes(path.join(OUT, "seq-1"));
    const b = hashes(path.join(OUT, "seq-2"));
    expect(a.size).toBe(426);
    expect([...b.keys()]).toEqual([...a.keys()]);
    const differing = [...a].filter(([name, hash]) => b.get(name) !== hash).map(([name]) => name);
    expect(differing, `${differing.length}/${a.size} frames differ: ${differing.slice(0, 10).join(", ")}`).toEqual([]);
  });

  it("two mp4 runs are byte-identical", () => {
    expect(sha256(path.join(OUT, "s1-2.mp4"))).toBe(sha256(path.join(OUT, "s1-1.mp4")));
  });
});
