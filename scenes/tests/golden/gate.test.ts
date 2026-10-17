/**
 * The publish gate on real renders (plan Phase 5), and the layout checker's
 * positive and negative controls in the pinned browser. Needs the browser
 * (`pnpm browser:ensure`) and ffmpeg/ffprobe on PATH.
 */
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import { afterAll, describe, expect, it } from "vitest";

import { inspectMarkup } from "../../src/adapters/remotion/inspect";
import { PACKAGE_ROOT } from "./cases";

const run = promisify(execFile);
const BIN = path.join(PACKAGE_ROOT, "bin/studio-scenes.mjs");
const dir = mkdtempSync(path.join(tmpdir(), "studio-gate-golden-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));
const sha256 = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");

const frame = (body: string) =>
  `<div data-studio-aspect="16:9" style="position:relative;width:1920px;height:1080px;overflow:hidden;background:#111115;font-family:'Studio Inter';color:#fff;font-size:40px">${body}</div>`;
const at = (x: number, y: number, style = "") => `position:absolute;left:${x}px;top:${y}px;white-space:nowrap;${style}`;

describe("the layout checker (controls)", () => {
  it("passes clean text and catches overflow, clipping, overlap; an ellipsis is truncation; text under an opaque box is not overlap", async () => {
    const result = await inspectMarkup(
      [
        { frame: 1, html: frame(`<div style="${at(100, 100)}">Clean text</div><div style="${at(100, 300)}">More clean text</div>`) },
        { frame: 2, html: frame(`<div style="${at(1700, 100)}">This line runs off the frame</div>`) },
        { frame: 3, html: frame(`<div style="${at(100, 100, "width:120px;overflow:hidden")}">Cut without an ellipsis</div>`) },
        { frame: 4, html: frame(`<div style="${at(100, 100, "width:120px;overflow:hidden;text-overflow:ellipsis")}">Shortened by design</div>`) },
        { frame: 5, html: frame(`<div style="${at(100, 100)}">First label</div><div style="${at(110, 105)}">Second label</div>`) },
        {
          frame: 6,
          html: frame(
            `<div style="${at(100, 100)}">Hidden below</div><div style="${at(90, 90, "z-index:2;background:#222;padding:20px")}">Card on top</div>`,
          ),
        },
      ],
      "16:9",
    );
    const kinds = (f: number) => result.problems.filter((p) => p.frame === f).map((p) => p.kind).sort();
    expect(result.emptyFrames).toEqual([]);
    // Eight visible runs: "Hidden below" is wholly covered, so it is not visible text.
    expect(result.textRuns).toBe(8);
    expect(kinds(1)).toEqual([]);
    expect(kinds(2)).toEqual(["outside-frame"]);
    expect(kinds(3)).toEqual(["clipped"]);
    expect(kinds(4)).toEqual(["truncated"]);
    expect(kinds(5)).toEqual(["overlap"]);
    expect(kinds(6)).toEqual([]);
  });

  it("does not let an ellipsis excuse a cut it did not make (review round 1 counter-examples)", async () => {
    const result = await inspectMarkup(
      [
        // An ellipsized label hanging off the frame: truncated by its box AND off the frame.
        { frame: 1, html: frame(`<div style="${at(1800, 100, "width:400px;overflow:hidden;text-overflow:ellipsis")}">A label that is long enough to be ellipsized here</div>`) },
        // text-overflow without overflow does nothing: the frame cuts it.
        { frame: 2, html: frame(`<div style="${at(1700, 100, "text-overflow:ellipsis")}">This line runs off the frame entirely</div>`) },
        // An ellipsis span inside a narrower card that cuts it with no ellipsis shown.
        { frame: 3, html: frame(`<div style="${at(100, 100, "width:150px;overflow:hidden")}"><span style="display:inline-block;width:600px;overflow:hidden;text-overflow:ellipsis">Cut by the card not the span</span></div>`) },
        // A box whose height cuts off the bottom of its line.
        { frame: 4, html: frame(`<div style="${at(100, 100, "height:30px;line-height:50px;overflow:hidden")}">Half a line</div>`) },
        // An opaque pill half over another label.
        { frame: 5, html: frame(`<div style="${at(100, 100)}">Underneath label text</div><div style="${at(250, 95, "background:#333;padding:4px;z-index:2")}">Pill</div>`) },
      ],
      "16:9",
    );
    const kinds = (f: number) => result.problems.filter((p) => p.frame === f).map((p) => p.kind).sort();
    expect(kinds(1)).toEqual(["outside-frame", "truncated"]);
    expect(kinds(2)).toEqual(["outside-frame"]);
    expect(kinds(3)).toEqual(["clipped"]);
    expect(kinds(4)).toEqual(["clipped"]);
    expect(kinds(5)).toEqual(["occluded"]);
  });
});

/** A mono 16-bit PCM WAV tone at -15 dBFS. */
function toneWav(seconds: number): Buffer {
  const rate = 24_000;
  const samples = Math.round(seconds * rate);
  const data = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 220 * i) / rate) * 5800), i * 2);
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

async function compose(out: string, ...extra: string[]) {
  await run(process.execPath, [BIN, "compose", "teaser", "--format", "mp4", "--aspect", "16:9", "--out", out, ...extra], {
    cwd: PACKAGE_ROOT,
    maxBuffer: 16 * 1024 * 1024,
  });
}

async function gate(video: string) {
  try {
    const { stdout } = await run(process.execPath, [BIN, "gate", video], { cwd: PACKAGE_ROOT, maxBuffer: 16 * 1024 * 1024 });
    return { status: 0, stdout, report: JSON.parse(readFileSync(`${video}.gate.json`, "utf8")) };
  } catch (err) {
    const e = err as { code: number; stdout: string };
    return { status: e.code, stdout: e.stdout, report: JSON.parse(readFileSync(`${video}.gate.json`, "utf8")) };
  }
}

describe("studio-scenes gate (CLI)", () => {
  it("passes a fully narrated, captioned teaser", async () => {
    const voice = path.join(dir, "voice");
    mkdirSync(voice);
    // Every segment narrated, so no silence reaches 10 s; words make captions.
    const lengths: Record<string, number> = { open: 2, lifecycle: 6, "merge-train": 8, "self-correction": 7, close: 2 };
    const segments: Record<string, unknown> = {};
    for (const [id, seconds] of Object.entries(lengths)) {
      writeFileSync(path.join(voice, `${id}.wav`), toneWav(seconds));
      segments[id] = {
        audio: `${id}.wav`,
        audio_sha256: sha256(path.join(voice, `${id}.wav`)),
        duration_seconds: seconds,
        words: [
          { text: "Narrated", start: 0, end: 0.6, matched: true },
          { text: `${id}.`, start: 0.6, end: 1.4, matched: true },
        ],
        speech: { status: "measured", by: "test fixture", match: 0.95, unheard: [] },
      };
    }
    const table = path.join(voice, "teaser.timing.json");
    writeFileSync(table, JSON.stringify({ timing_version: 1, composition: "teaser", script_sha256: "0".repeat(64), voice: "test tone", segments }));
    const video = path.join(dir, "narrated.mp4");
    await compose(video, "--narration", table);
    const { status, report, stdout } = await gate(video);
    expect(stdout).toContain("PASS");
    expect(report.failed).toEqual([]);
    expect(report.checks.map((c: { name: string }) => c.name)).toEqual(["manifest", "speech", "layout", "ffprobe", "frames", "audio", "captions"]);
    expect(status).toBe(0);
  });

  it("blocks a silent teaser, and records exactly why", async () => {
    const video = path.join(dir, "silent.mp4");
    await compose(video);
    const { status, report } = await gate(video);
    expect(status).toBe(3);
    expect(report.verdict).toBe("fail");
    expect(report.failed.sort()).toEqual(["audio", "captions", "speech"]);
  });

  it("blocks a video that no longer matches its manifest", async () => {
    // Its own render, so the test does not depend on another test's output.
    const own = path.join(dir, "tampered");
    mkdirSync(own);
    const video = path.join(own, "teaser.mp4");
    await compose(video);
    writeFileSync(video, Buffer.concat([readFileSync(video), Buffer.from([0])]));
    const { status, report } = await gate(video);
    expect(status).toBe(3);
    expect(report.checks.find((c: { name: string }) => c.name === "manifest").detail).toMatch(/does not hash/);
  });
});
