/**
 * Compositions through the CLI (plan Phase 5): the teaser renders with zero
 * b-roll (the slot is omitted and the manifest says so), and with a b-roll
 * still and a narration clip bound it renders byte-identically twice, with
 * its audio mixed in and its captions written beside it.
 */
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import { getVideoMetadata } from "@remotion/renderer";
import { PNG } from "pngjs";
import { afterAll, describe, expect, it } from "vitest";

import { STUDIO_FPS } from "../../src/api/layout";
import { PACKAGE_ROOT } from "./cases";

const run = promisify(execFile);
const BIN = path.join(PACKAGE_ROOT, "bin/studio-scenes.mjs");
const dir = mkdtempSync(path.join(tmpdir(), "studio-compose-golden-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

const sha256 = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");

async function compose(out: string, ...extra: string[]) {
  const { stdout } = await run(
    process.execPath,
    [BIN, "compose", "teaser", "--format", "mp4", "--aspect", "16:9", "--out", out, ...extra],
    { cwd: PACKAGE_ROOT, maxBuffer: 16 * 1024 * 1024 },
  );
  return { stdout, manifest: JSON.parse(readFileSync(`${out}.composition.json`, "utf8")) };
}

/** A mono 16-bit PCM WAV: a quiet tone, `seconds` long. */
function toneWav(seconds: number): Buffer {
  const rate = 24_000;
  const samples = Math.round(seconds * rate);
  const data = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 220 * i) / rate) * 6000), i * 2);
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + data.length, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(data.length, 40);
  return Buffer.concat([header, data]);
}

function gradientPng(width: number, height: number): Buffer {
  const png = new PNG({ width, height });
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = (y * width + x) * 4;
      png.data[i] = Math.round((x / width) * 120) + 40;
      png.data[i + 1] = 60;
      png.data[i + 2] = Math.round((y / height) * 120) + 60;
      png.data[i + 3] = 255;
    }
  }
  return PNG.sync.write(png);
}

describe("compose (CLI)", () => {
  it("renders the teaser with zero b-roll; the empty slot is omitted and recorded", async () => {
    const out = path.join(dir, "plain.mp4");
    const { manifest } = await compose(out);
    expect(manifest.omitted).toEqual([{ id: "broll", reason: 'b-roll slot "teaser-closing" has no image bound' }]);
    expect(manifest.narration).toBeNull();
    expect(manifest.captions).toBeNull();
    expect(manifest.output.sha256).toBe(sha256(out));
    expect(manifest.segments.every((s: { mode: string }) => s.mode === "illustrative")).toBe(true);
    const meta = await getVideoMetadata(out, { logLevel: "error" });
    expect(meta.width).toBe(1920);
    expect(meta.height).toBe(1080);
    expect(meta.fps).toBe(STUDIO_FPS);
    expect(Math.round((meta.durationInSeconds ?? 0) * STUDIO_FPS)).toBe(manifest.duration_in_frames);
  });

  it("with b-roll and narration bound, renders byte-identically twice, with audio and captions", async () => {
    const still = path.join(dir, "still.png");
    writeFileSync(still, gradientPng(1920, 1080));
    writeFileSync(
      `${still}.provenance.json`,
      JSON.stringify({ ai_generated: true, provider: { id: "test-fixture", adapter: "fake" }, file: { sha256: sha256(still) } }),
    );
    const voice = path.join(dir, "voice");
    rmSync(voice, { recursive: true, force: true });
    mkdirSync(voice);
    const clip = path.join(voice, "open.wav");
    writeFileSync(clip, toneWav(2));
    const table = path.join(voice, "teaser.timing.json");
    writeFileSync(
      table,
      JSON.stringify({
        timing_version: 1,
        composition: "teaser",
        script_sha256: "0".repeat(64),
        voice: "test tone",
        segments: {
          open: {
            audio: "open.wav",
            audio_sha256: sha256(clip),
            duration_seconds: 2,
            words: [
              { text: "Meet", start: 0, end: 0.4, matched: true },
              { text: "Qontinui.", start: 0.4, end: 1.2, matched: true },
            ],
          },
        },
      }),
    );
    const args = ["--broll", `teaser-closing=${still}`, "--narration", table];
    const a = await compose(path.join(dir, "a.mp4"), ...args);
    const b = await compose(path.join(dir, "b.mp4"), ...args);
    expect(sha256(path.join(dir, "a.mp4"))).toBe(sha256(path.join(dir, "b.mp4")));
    expect(a.manifest.omitted).toEqual([]);
    expect(a.manifest.broll).toEqual([expect.objectContaining({ slot: "teaser-closing", provider: "test-fixture" })]);
    expect(a.manifest.narration.clips).toEqual([expect.objectContaining({ segment: "open", start_frame: 8, duration_in_frames: 60 })]);
    expect(a.manifest.captions).toEqual({ file: "a.mp4.vtt", cues: 1 });
    expect(readFileSync(path.join(dir, "a.mp4.vtt"), "utf8")).toContain("Meet Qontinui.");
    expect(b.manifest.segments).toEqual(a.manifest.segments);
    const meta = await getVideoMetadata(path.join(dir, "a.mp4"), { logLevel: "error" });
    expect(meta.audioCodec).not.toBeNull();
    // The container may run a frame long: the AAC encoder's priming samples.
    expect(Math.abs(Math.round((meta.durationInSeconds ?? 0) * STUDIO_FPS) - a.manifest.duration_in_frames)).toBeLessThanOrEqual(1);
  });
});
