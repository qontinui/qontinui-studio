/**
 * Review fixes: replay provenance (D2), glyph coverage, S1 shape checks,
 * scale rules, output safety and the untrusting player.
 */
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { describe, expect, it } from "vitest";

import {
  REPLAY_REFUSAL,
  SOURCE_REF_MAX,
  isCovered,
  missingRequirements,
  parseTrace,
  renderRefusal,
  scaleProblem,
  uncoveredCharacters,
  validateTrace,
} from "../../src/api";
import { attestReplayForTests } from "../../src/api/trace/testing";
import {
  OutputRefused,
  checkFileTarget,
  checkSequenceTarget,
  publishSequence,
  withStaging,
} from "../../src/adapters/output";
import { StudioPlayer } from "../../src/adapters/remotion/Player";
import { s1PlanLifecycle } from "../../src/scenes/s1-plan-lifecycle";
import { clone, replayTraceJson, s1TraceJson } from "./fixtures";

describe("replay provenance (plan D2)", () => {
  it("refuses to render a hand-written replay", () => {
    const trace = parseTrace(replayTraceJson());
    expect(renderRefusal(trace)).toBe(REPLAY_REFUSAL);
    expect(REPLAY_REFUSAL).toMatch(/--build-record/);
  });

  it("renders illustrative traces", () => {
    expect(renderRefusal(parseTrace(s1TraceJson()))).toBeNull();
  });

  it("a replay is renderable only as the attested object, not a copy of it", () => {
    const trace = attestReplayForTests(parseTrace(replayTraceJson()));
    expect(renderRefusal(trace)).toBeNull();
    expect(renderRefusal(structuredClone(trace))).toBe(REPLAY_REFUSAL);
    expect(renderRefusal(parseTrace(JSON.parse(JSON.stringify(trace))))).toBe(REPLAY_REFUSAL);
  });
});

describe("glyph coverage", () => {
  it("covers latin, latin-ext and arrows; not CJK", () => {
    for (const ch of ["A", "é", "ł", "–", "→", "·"]) expect(isCovered(ch.codePointAt(0) as number), ch).toBe(true);
    expect(isCovered("日".codePointAt(0) as number)).toBe(false);
    expect(uncoveredCharacters("plan → 日本語 → 日")).toEqual(["日", "本", "語"]);
  });

  it("refuses a trace string the fonts cannot draw, naming the character", () => {
    const json = clone(s1TraceJson());
    json.entities!.plans![0]!.title = "Ship → 日本語";
    const result = validateTrace(json);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors).toContain(
        'entities.plans.0.title: characters the bundled fonts cannot draw: "日" (U+65E5), "本" (U+672C), "語" (U+8A9E)',
      );
    }
  });

  it("accepts an arrow", () => {
    const json = clone(s1TraceJson());
    json.entities!.plans![0]!.title = "Draft → shipped";
    expect(validateTrace(json).ok).toBe(true);
  });
});

describe("schema caps", () => {
  it(`caps source.ref at ${SOURCE_REF_MAX} characters`, () => {
    const json = clone(replayTraceJson());
    json.meta.source.ref = "x".repeat(SOURCE_REF_MAX);
    expect(validateTrace(json).ok).toBe(true);
    json.meta.source.ref = "x".repeat(SOURCE_REF_MAX + 1);
    expect(validateTrace(json).ok).toBe(false);
  });
});

describe("S1 shape checks", () => {
  it("needs exactly one plan and at most one PR", () => {
    const twoPlans = clone(s1TraceJson());
    twoPlans.entities!.plans!.push({ id: "plan-2", title: "Another" });
    expect(missingRequirements(s1PlanLifecycle, parseTrace(twoPlans))).toContain("exactly one plan (the trace has 2)");

    const twoPrs = clone(s1TraceJson());
    twoPrs.entities!.prs!.push({ id: "pr-2", plan: "plan-retry-budget", repo: "example/x", number: 2, title: "t" });
    expect(missingRequirements(s1PlanLifecycle, parseTrace(twoPrs))).toContain("at most one PR (the trace has 2)");
  });
});

describe("scale", () => {
  it("accepts scales that give even whole pixels at every aspect", () => {
    for (const s of [0.1, 0.25, 0.3, 0.4, 0.5, 1, 2]) expect(scaleProblem(s), String(s)).toBeNull();
  });
  it("refuses odd or fractional sizes and out-of-range values", () => {
    expect(scaleProblem(0.025)).toMatch(/48x27 at 16:9/); // odd height
    expect(scaleProblem(0.33)).toMatch(/even whole pixels/);
    expect(scaleProblem(0)).toMatch(/\(0, 4\]/);
    expect(scaleProblem(5)).toMatch(/\(0, 4\]/);
  });
});

describe("output safety", () => {
  const tmp = () => mkdtempSync(path.join(tmpdir(), "studio-out-"));

  it("refuses a sequence dir holding foreign files, and deletes nothing", async () => {
    const dir = path.join(tmp(), "seq");
    mkdirSync(path.join(dir, "sub"), { recursive: true });
    writeFileSync(path.join(dir, "notes.txt"), "mine");
    writeFileSync(path.join(dir, "sub", "keep.md"), "mine");
    await expect(checkSequenceTarget(dir, true)).rejects.toThrow(OutputRefused);
    await expect(checkSequenceTarget(dir, true)).rejects.toThrow(/notes\.txt, sub/);
    expect(readFileSync(path.join(dir, "notes.txt"), "utf8")).toBe("mine");
    expect(readFileSync(path.join(dir, "sub", "keep.md"), "utf8")).toBe("mine");
  });

  it("replaces a previous sequence only with overwrite, and only those files", async () => {
    const root = tmp();
    const dir = path.join(root, "seq");
    mkdirSync(dir);
    for (const n of ["element-000.png", "element-001.png", "element-002.png"]) writeFileSync(path.join(dir, n), "old");
    await expect(checkSequenceTarget(dir, false)).rejects.toThrow(/--force/);
    await withStaging(dir, async (staging) => {
      writeFileSync(path.join(staging, "element-000.png"), "new");
      writeFileSync(path.join(staging, "element-001.png"), "new");
      writeFileSync(path.join(staging, "stray.tmp"), "x");
      const published = await publishSequence(staging, dir, true);
      expect(published.map((p) => path.basename(p))).toEqual(["element-000.png", "element-001.png"]);
    });
    expect(readdirSync(dir).sort()).toEqual(["element-000.png", "element-001.png"]);
    expect(readFileSync(path.join(dir, "element-000.png"), "utf8")).toBe("new");
    // The staging directory is gone; nothing else was created beside the target.
    expect(readdirSync(root)).toEqual(["seq"]);
  });

  it("refuses an existing file without overwrite and a directory always", async () => {
    const root = tmp();
    const file = path.join(root, "out.mp4");
    writeFileSync(file, "existing");
    await expect(checkFileTarget(file, false)).rejects.toThrow(/--force/);
    await expect(checkFileTarget(file, true)).resolves.toBeUndefined();
    await expect(checkFileTarget(root, true)).rejects.toThrow(/not a regular file/);
    expect(readFileSync(file, "utf8")).toBe("existing");
  });

  it("cleans up staging even when the render fails", async () => {
    const root = tmp();
    await expect(
      withStaging(path.join(root, "out.mp4"), async (staging) => {
        writeFileSync(path.join(staging, "partial.mp4"), "x");
        throw new Error("render failed");
      }),
    ).rejects.toThrow("render failed");
    expect(readdirSync(root)).toEqual([]);
    expect(existsSync(path.join(root, "out.mp4"))).toBe(false);
  });
});

describe("StudioPlayer does not take the trace on trust", () => {
  const html = (trace: unknown) =>
    renderToStaticMarkup(createElement(StudioPlayer, { scene: "s1-plan-lifecycle", trace, aspect: "16:9", assetBaseUrl: "/x" }));

  it("renders an error panel for an invalid trace", () => {
    const out = html({ trace_version: 1 });
    expect(out).toContain("Scene cannot be shown");
    expect(out).toContain("meta");
  });

  it("refuses a hand-written replay", () => {
    expect(html(replayTraceJson())).toContain("refusing to render a replay trace");
  });

  it("refuses a trace that cannot drive the scene", () => {
    const json = clone(s1TraceJson());
    json.entities!.plans!.push({ id: "plan-2", title: "Another" });
    expect(html(json)).toContain("exactly one plan");
  });

  it("renders the player for a valid illustrative trace", () => {
    expect(html(s1TraceJson())).not.toContain("Scene cannot be shown");
  });
});

describe("glyph table matches the bundled fonts", () => {
  it("records the SHA-256 of every font file in assets/fonts", async () => {
    const { createHash } = await import("node:crypto");
    const { FONT_SHA256 } = await import("../../src/api/glyph-coverage");
    const { fontFiles } = await import("../../src/api/typography");
    const dir = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../assets/fonts");
    const onDisk = readdirSync(dir).filter((f) => f.endsWith(".woff2")).sort();
    expect(Object.keys(FONT_SHA256).sort(), "fonts on disk vs glyph table").toEqual(onDisk);
    expect(fontFiles.map((f) => path.basename(f.file)).sort(), "fonts loaded vs glyph table").toEqual(onDisk);
    for (const name of onDisk) {
      const digest = createHash("sha256").update(readFileSync(path.join(dir, name))).digest("hex");
      expect(digest, `${name} changed; run scripts/gen-glyph-coverage.py`).toBe(FONT_SHA256[name]);
    }
  });
});
