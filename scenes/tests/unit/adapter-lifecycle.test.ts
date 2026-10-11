/**
 * Adapter lifecycle without an engine: an adapter is single-use after close()
 * (src/api/engine.ts), close() is idempotent, and the Remotion adapter strips
 * only Remotion's own exit-on-SIGINT listener. No browser and no Docker are
 * started: close() comes first, and the HyperFrames adapter is given a
 * docker binary that does not exist.
 */
import { mkdtempSync, readFileSync, readdirSync, rmdirSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterAll, describe, expect, it } from "vitest";

import { HyperFramesAdapter } from "../../src/adapters/hyperframes/render";
import { REMOTION_SIGINT_EXIT, RemotionAdapter, isCancelNoise, removeRemotionSigintExit } from "../../src/adapters/remotion/render";
import { AdapterClosed, type EngineAdapter } from "../../src/api/engine";
import { parseTrace } from "../../src/api/trace/schema";
import { s1TraceJson } from "./fixtures";

const dirs: string[] = [];
const tmp = () => {
  const dir = mkdtempSync(path.join(tmpdir(), "studio-lifecycle-"));
  dirs.push(dir);
  return dir;
};
afterAll(() => {
  for (const dir of dirs) rmdirSync(dir); // fails if a refused call wrote anything
});

const adapters: [string, () => EngineAdapter][] = [
  ["hyperframes", () => new HyperFramesAdapter({ docker: "/nonexistent/studio-test-no-docker" })],
  ["remotion", () => new RemotionAdapter({ serveUrl: "http://127.0.0.1:9/unused", port: 9 })],
];

describe.each(adapters)("%s adapter is single-use after close()", (_name, make) => {
  it("refuses render and renderStills after close(), writing nothing", async () => {
    const adapter = make();
    await adapter.close();
    const dir = tmp();
    const trace = parseTrace(s1TraceJson());
    await expect(
      adapter.render({ scene: "s1-plan-lifecycle", trace, format: "mp4", aspect: "16:9", outPath: path.join(dir, "x.mp4") }),
    ).rejects.toBeInstanceOf(AdapterClosed);
    await expect(
      adapter.renderStills({ scene: "s1-plan-lifecycle", trace, aspect: "16:9", frames: [0], outDir: dir }),
    ).rejects.toBeInstanceOf(AdapterClosed);
    expect(readdirSync(dir)).toEqual([]);
  });

  it("close() is idempotent", async () => {
    const adapter = make();
    await Promise.all([adapter.close(), adapter.close()]);
    await expect(adapter.close()).resolves.toBeUndefined();
  });
});

describe("Remotion SIGINT listener handling", () => {
  it("removes only Remotion's exit-on-SIGINT listener, identified by its source", () => {
    const killProcess = () => undefined;
    // Same shape as @remotion/renderer BrowserRunner.js's listener; never called.
    const remotionLike = () => {
      killProcess();
      process.exit(130);
    };
    const someoneElse = () => undefined;
    process.on("SIGINT", remotionLike);
    process.on("SIGINT", someoneElse);
    try {
      removeRemotionSigintExit();
      expect(process.listeners("SIGINT")).not.toContain(remotionLike);
      expect(process.listeners("SIGINT")).toContain(someoneElse);
    } finally {
      process.off("SIGINT", remotionLike);
      process.off("SIGINT", someoneElse);
    }
  });

  it("matches the listener in the installed @remotion/renderer (fails if Remotion changes it)", () => {
    const require = createRequire(import.meta.url);
    const runner = path.join(path.dirname(require.resolve("@remotion/renderer")), "browser/BrowserRunner.js");
    const source = readFileSync(runner, "utf8");
    const sigint = source.slice(source.indexOf("'SIGINT'"), source.indexOf("'SIGINT'") + 200);
    expect(sigint).toMatch(REMOTION_SIGINT_EXIT);
  });
});

describe("Remotion cancel noise", () => {
  it("absorbs only EPIPE and closed-target/session protocol errors", () => {
    expect(isCancelNoise(Object.assign(new Error("write EPIPE"), { code: "EPIPE" }))).toBe(true);
    expect(isCancelNoise(new Error("Protocol error (Target.closeTarget): Target closed. https://www.remotion.dev/docs/target-closed"))).toBe(true);
    expect(isCancelNoise(new Error("Protocol error (Runtime.callFunctionOn): Session closed."))).toBe(true);
    expect(isCancelNoise(Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }))).toBe(false);
    expect(isCancelNoise(new Error("something else"))).toBe(false);
    expect(isCancelNoise("EPIPE")).toBe(false);
    expect(isCancelNoise(null)).toBe(false);
  });
});
