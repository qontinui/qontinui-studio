/**
 * Adapter lifecycle without an engine: an adapter is single-use after close()
 * (src/api/engine.ts), close() is idempotent, and the Remotion adapter strips
 * only Remotion's own exit-on-SIGINT listener. No browser and no Docker are
 * started: close() comes first, and the HyperFrames adapter is given a
 * docker binary that does not exist.
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmdirSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";

import { HyperFramesAdapter } from "../../src/adapters/hyperframes/render";
import { REMOTION_SIGINT_EXIT, RemotionAdapter, removeRemotionSigintExit, unhandledRejectionsMode } from "../../src/adapters/remotion/render";
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
  const CHILD = path.join(path.dirname(fileURLToPath(import.meta.url)), "cancel-noise-child.ts");
  /** `spelling`: "=" passes `--unhandled-rejections=<mode>`, " " passes it as two arguments, "env" via NODE_OPTIONS. */
  const run = (where: "inside" | "outside", mode?: string, spelling: "=" | " " | "env" = "=") => {
    const flag = mode === undefined ? [] : spelling === "=" ? [`--unhandled-rejections=${mode}`] : spelling === " " ? ["--unhandled-rejections", mode] : [];
    const env = mode !== undefined && spelling === "env" ? { ...process.env, NODE_OPTIONS: `--unhandled-rejections ${mode}` } : process.env;
    return spawnSync(process.execPath, [...flag, "--import", "tsx", CHILD, where], { encoding: "utf8", timeout: 60_000, env });
  };
  const count = (text: string, needle: string) => text.split(needle).length - 1;

  it("absorbs a rejection from a cancelled render's own async context", () => {
    const r = run("inside");
    expect(r.status).toBe(0);
    expect(r.stdout).toContain("alive");
    expect(r.stderr).not.toContain("EPIPE");
  });

  it("leaves the caller's own rejection to Node: crashes in the default throw mode", () => {
    const r = run("outside");
    expect(r.status).toBe(1);
    expect(r.stdout).not.toContain("alive");
    expect(r.stderr).toContain("write EPIPE");
  });

  it.each(["=", " ", "env"] as const)(
    "leaves the caller's own rejection to Node in warn mode (spelling %j): one warning, no crash",
    (spelling) => {
      const warn = run("outside", "warn", spelling);
      expect(warn.status).toBe(0);
      expect(warn.stdout).toContain("alive");
      expect(count(warn.stderr, "UnhandledPromiseRejectionWarning: Error: write EPIPE")).toBe(1);
    },
  );

  it("leaves the caller's own rejection to Node: exit 1 in warn-with-error-code, nothing in none", () => {
    const code = run("outside", "warn-with-error-code");
    expect(code.status).toBe(1);
    expect(code.stdout).toContain("alive");
    expect(count(code.stderr, "UnhandledPromiseRejectionWarning: Error: write EPIPE")).toBe(1);
    const none = run("outside", "none");
    expect(none.status).toBe(0);
    expect(none.stderr).not.toContain("EPIPE");
  });

  it("reads the --unhandled-rejections mode from execArgv and NODE_OPTIONS", () => {
    expect(unhandledRejectionsMode([], "")).toBe("throw");
    expect(unhandledRejectionsMode(["--unhandled-rejections=warn"], "")).toBe("warn");
    expect(unhandledRejectionsMode([], "--max-old-space-size=4096 --unhandled-rejections=none")).toBe("none");
    expect(unhandledRejectionsMode(["--unhandled-rejections=strict"], "--unhandled-rejections=none")).toBe("strict");
    expect(unhandledRejectionsMode(["--unhandled-rejections", "warn"], "")).toBe("warn");
    expect(unhandledRejectionsMode([], "--unhandled-rejections warn")).toBe("warn");
    expect(unhandledRejectionsMode(["--import", "tsx", "--unhandled-rejections", "none"], "--unhandled-rejections=warn")).toBe("none");
  });
});

describe("an un-awaited failing render", () => {
  const CHILD = path.join(path.dirname(fileURLToPath(import.meta.url)), "unawaited-render-child.ts");
  it.each(["remotion", "hyperframes"])("%s: still surfaces as an unhandled rejection in Node's default mode", (engine) => {
    const r = spawnSync(process.execPath, ["--import", "tsx", CHILD, engine], { encoding: "utf8", timeout: 60_000 });
    expect(r.status).toBe(1);
    expect(r.stdout).not.toContain("alive");
  });
});
