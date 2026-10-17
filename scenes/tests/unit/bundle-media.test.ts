/**
 * bundleWithMedia (plan Phase 5): the one new code path that deletes files.
 * It stages the bundled assets and the given media in a directory it creates,
 * hands that to the bundler, and afterwards removes exactly what it wrote —
 * also when the bundler fails — never touching a source file.
 */
import { existsSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { bundleWithMedia } from "../../src/adapters/remotion/render";

function source(name: string, content: string): string {
  const file = path.join(mkdtempSync(path.join(tmpdir(), "studio-media-src-")), name);
  writeFileSync(file, content);
  return file;
}

describe("bundleWithMedia", () => {
  it("serves the fonts and the media to the bundler, then removes its staging", async () => {
    const clip = source("a.wav", "audio");
    let seen: { dir: string; files: string[] } | null = null;
    const url = await bundleWithMedia({ "media/abc.wav": clip }, async (publicDir) => {
      seen = { dir: publicDir, files: readdirSync(publicDir, { recursive: true }).map(String).sort() };
      expect(readFileSync(path.join(publicDir, "media/abc.wav"), "utf8")).toBe("audio");
      return "bundle-url";
    });
    expect(url).toBe("bundle-url");
    expect(seen!.files).toEqual(expect.arrayContaining(["fonts", "media", path.join("media", "abc.wav"), path.join("fonts", "Inter-Regular.woff2")]));
    expect(existsSync(path.dirname(seen!.dir))).toBe(false);
    expect(readFileSync(clip, "utf8")).toBe("audio");
  });

  it("removes its staging when the bundler fails", async () => {
    let dir = "";
    await expect(
      bundleWithMedia({ "media/x.png": source("x.png", "img") }, async (publicDir) => {
        dir = publicDir;
        throw new Error("bundler broke");
      }),
    ).rejects.toThrow("bundler broke");
    expect(existsSync(path.dirname(dir))).toBe(false);
  });

  it("refuses a media name that is not media/<file>, before writing anything", async () => {
    await expect(bundleWithMedia({ "fonts/Inter-Regular.woff2": source("f", "x") }, async () => "never")).rejects.toThrow(/must look like media/);
    await expect(bundleWithMedia({ "media/../x": source("f", "x") }, async () => "never")).rejects.toThrow(/must look like media/);
  });
});
