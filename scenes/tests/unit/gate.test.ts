/**
 * The publish gate's judgements (plan Phase 5), on data: no ffmpeg, no
 * browser. tests/golden/gate.test.ts runs the gate on real renders.
 */
import { describe, expect, it } from "vitest";

import {
  DEFAULT_THRESHOLDS,
  checkAudio,
  checkCaptions,
  checkFrames,
  checkLayout,
  checkProbe,
  checkSpeech,
  parseVtt,
  samplePoints,
} from "../../src/gate/checks";
import { layoutFrames, runGate } from "../../src/gate/gate";
import type { CompositionManifest } from "../../src/gate/manifest";
import { lumaStats, parseAudioLog, type Probe } from "../../src/gate/probe";
import type { NarrationTiming } from "../../src/api/narration-timing";
import type { Program } from "../../src/api/composition";

const SHA = "a".repeat(64);

function manifest(overrides: Partial<CompositionManifest> = {}): CompositionManifest {
  return {
    manifest_version: 1,
    composition: "teaser",
    title: "t",
    aspect: "16:9",
    format: "mp4",
    fps: 30,
    width: 1920,
    height: 1080,
    duration_in_frames: 300,
    output: { file: "v.mp4", sha256: SHA },
    captions: { file: "v.mp4.vtt", cues: 2 },
    variables: {},
    segments: [],
    omitted: [],
    notes: [],
    traces: [],
    broll: [],
    narration: {
      file: "/x/teaser.timing.json",
      sha256: SHA,
      voice: "v",
      script_sha256: SHA,
      clips: [
        { segment: "a", start_frame: 8, duration_in_frames: 60, audio_sha256: SHA, speech: null },
        { segment: "b", start_frame: 150, duration_in_frames: 60, audio_sha256: SHA, speech: null },
      ],
    },
    engine: "remotion",
    ...overrides,
  } as CompositionManifest;
}

const goodProbe: Probe = {
  format: { duration: "10.000000" },
  streams: [
    { codec_type: "video", codec_name: "h264", width: 1920, height: 1080, pix_fmt: "yuvj420p", avg_frame_rate: "30/1", nb_frames: "300" },
    { codec_type: "audio", codec_name: "aac", sample_rate: "48000", channels: 2 },
  ],
};

describe("ffprobe sanity", () => {
  it("passes a file that is what the manifest says", () => {
    expect(checkProbe(manifest(), goodProbe).status).toBe("pass");
  });
  it("reads a webm's video length from its packet count (Matroska reports no nb_frames)", () => {
    const webm: Probe = {
      format: { duration: "10.000000" },
      streams: [
        { codec_type: "video", codec_name: "vp8", width: 1920, height: 1080, pix_fmt: "yuv420p", avg_frame_rate: "30/1", nb_read_packets: "300" },
        { codec_type: "audio", codec_name: "opus" },
      ],
    };
    expect(checkProbe(manifest({ format: "webm" }), webm).status).toBe("pass");
    const noLength: Probe = { ...webm, streams: [{ ...webm.streams![0]!, nb_read_packets: undefined }, webm.streams![1]!] };
    expect(checkProbe(manifest({ format: "webm" }), noLength).detail).toMatch(/length could not be read/);
  });

  it("fails a video stream shorter than the container (audio running long)", () => {
    const short: Probe = { ...goodProbe, streams: [{ ...goodProbe.streams![0]!, nb_frames: "200" }, goodProbe.streams![1]!] };
    expect(checkProbe(manifest(), short).detail).toMatch(/the video stream has 200 frames, expected 300/);
    const zero: Probe = { ...goodProbe, streams: [{ ...goodProbe.streams![0]!, avg_frame_rate: "0/0", r_frame_rate: "30/1" }, goodProbe.streams![1]!] };
    expect(checkProbe(manifest(), zero).status).toBe("pass");
  });

  it("fails a wrong size, codec, frame rate, duration or a missing audio stream", () => {
    const bad: Probe = {
      format: { duration: "12" },
      streams: [{ codec_type: "video", codec_name: "hevc", width: 1280, height: 720, pix_fmt: "yuv444p", avg_frame_rate: "25/1" }],
    };
    const r = checkProbe(manifest(), bad);
    expect(r.status).toBe("fail");
    for (const what of ["duration 12.000", "video codec hevc", "size 1280x720", "frame rate 25/1", "pixel format yuv444p", "0 audio streams"]) {
      expect(r.detail).toContain(what);
    }
  });
});

describe("black frames", () => {
  const frame = (at: number, peak: number, stddev: number) => ({ at, mean: 20, peak, stddev });
  it("samples the middles of the four quarters", () => {
    expect(samplePoints(40)).toEqual([5, 15, 25, 35]);
  });
  it("fails a black or flat frame and needs exactly four", () => {
    expect(checkFrames([frame(1, 200, 30), frame(2, 200, 30), frame(3, 200, 30), frame(4, 200, 30)]).status).toBe("pass");
    expect(checkFrames([frame(1, 200, 30), frame(2, 16, 0.5), frame(3, 200, 0.5), frame(4, 200, 30)]).detail).toMatch(/2\.00 s is black.*3\.00 s is a flat field/);
    // A dark frame with a little lit text is not black.
    expect(checkFrames([frame(1, 200, 3), frame(2, 200, 3), frame(3, 200, 3), frame(4, 200, 3)]).status).toBe("pass");
    expect(checkFrames([frame(1, 200, 30)]).status).toBe("fail");
  });
  it("computes luma statistics", () => {
    const black = lumaStats(new Uint8Array(1000).fill(16));
    expect(black).toEqual({ mean: 16, stddev: 0, peak: 16 });
    const lit = new Uint8Array(10000).fill(17);
    lit.fill(255, 0, 20);
    expect(lumaStats(lit).peak).toBe(255);
  });
});

describe("audio silence and clipping", () => {
  const log = `[silencedetect @ 0x1] silence_start: 3.2
[silencedetect @ 0x1] silence_end: 15.5 | silence_duration: 12.3
[Parsed_volumedetect_1 @ 0x2] mean_volume: -24.1 dB
[Parsed_volumedetect_1 @ 0x2] max_volume: -0.0 dB`;
  it("parses ffmpeg's silencedetect and volumedetect log", () => {
    expect(parseAudioLog(log)).toEqual({ meanVolume: -24.1, maxVolume: -0, silences: [{ start: 3.2, end: 15.5 }] });
  });
  it("fails no audio, a silent track, a long silence (also one running to the end) and clipping", () => {
    expect(checkAudio(null, 30).detail).toMatch(/no audio stream/);
    const r = checkAudio(parseAudioLog(log), 30);
    expect(r.detail).toMatch(/12\.3 s of silence from 3\.2 s/);
    expect(r.detail).toMatch(/clipping: peak 0 dB/);
    expect(checkAudio({ meanVolume: -80, maxVolume: -40, silences: [] }, 30).detail).toMatch(/silent/);
    expect(checkAudio({ meanVolume: -20, maxVolume: -3, silences: [{ start: 18, end: null }] }, 30).detail).toMatch(/12\.0 s of silence/);
    expect(checkAudio({ meanVolume: -20, maxVolume: -3, silences: [{ start: 2, end: 5 }] }, 30).status).toBe("pass");
  });
});

describe("captions", () => {
  const vtt = "WEBVTT\n\n1\n00:00:00.300 --> 00:00:01.500\nA plan ships.\n\n2\n00:00:05.100 --> 00:00:06.000\nThen it lands.\n";
  it("parses WebVTT and refuses a malformed file", () => {
    expect(parseVtt(vtt)).toEqual([
      { start: 0.3, end: 1.5, text: "A plan ships." },
      { start: 5.1, end: 6, text: "Then it lands." },
    ]);
    expect(() => parseVtt("1\n00:00 --> x\nhi")).toThrow(/WEBVTT/);
    expect(() => parseVtt("WEBVTT\n\n1\n00:00:01.000 --> nope\nhi")).toThrow(/bad cue timing/);
  });
  const clips = manifest().narration!.clips;
  it("passes the exact expected captions covering every narrated segment", () => {
    expect(checkCaptions(manifest(), clips, vtt, vtt, 10).status).toBe("pass");
  });
  it("fails a missing, stale or edited file, a count mismatch, an uncovered segment and no narration", () => {
    expect(checkCaptions(manifest(), clips, null, vtt, 10).detail).toMatch(/missing/);
    expect(checkCaptions(manifest(), clips, vtt.replace("ships", "slips"), vtt, 10).detail).toMatch(/stale or edited/);
    expect(checkCaptions(manifest({ captions: { file: "v.mp4.vtt", cues: 5 } }), clips, vtt, vtt, 10).detail).toMatch(/2 cues, the manifest says 5/);
    const one = "WEBVTT\n\n1\n00:00:00.300 --> 00:00:01.500\nA plan ships.\n";
    expect(checkCaptions(manifest({ captions: { file: "v.mp4.vtt", cues: 1 } }), clips, one, one, 10).detail).toMatch(/no caption while segment "b"/);
    expect(checkCaptions(manifest(), [], vtt, vtt, 10).detail).toMatch(/no narration/);
  });
});

describe("speech match", () => {
  const timing = (a: unknown, b: unknown): NarrationTiming =>
    ({
      timing_version: 1,
      composition: "teaser",
      script_sha256: SHA,
      voice: "v",
      segments: {
        a: { audio: "a.wav", audio_sha256: SHA, duration_seconds: 2, marks: {}, words: [], speech: a },
        b: { audio: "b.wav", audio_sha256: SHA, duration_seconds: 2, marks: {}, words: [], speech: b },
      },
    }) as NarrationTiming;
  const measured = (match: number) => ({ status: "measured", by: "whisper", match, unheard: match < 1 ? ["lands."] : [] });
  it("passes when every narrated segment reaches 0.9", () => {
    expect(checkSpeech(manifest().narration!.clips, timing(measured(1), measured(0.9))).status).toBe("pass");
  });
  it("fails a low match, an unavailable check, a never-checked segment and no narration", () => {
    const clips = manifest().narration!.clips;
    expect(checkSpeech(clips, timing(measured(1), measured(0.89))).detail).toMatch(/b: speech match 0\.890 < 0\.9 \(unheard: lands\.\)/);
    expect(checkSpeech(clips, timing(measured(1), { status: "unavailable", reason: "no ASR" })).detail).toMatch(/unavailable \(no ASR\)/);
    expect(checkSpeech(clips, timing(measured(1), undefined)).detail).toMatch(/b: never checked/);
    expect(checkSpeech([], null).status).toBe("fail");
  });
});

describe("layout", () => {
  const p = (kind: string) => ({ frame: 10, segment: "s", kind, text: "x", detail: "d" });
  it("fails overflow, clipping and overlap; a designed ellipsis is only a warning; nothing inspected fails", () => {
    expect(checkLayout({ frames: [10], textRuns: 5, problems: [p("truncated")] })).toMatchObject({ status: "pass", warnings: [expect.stringMatching(/truncated/)] });
    for (const kind of ["outside-frame", "clipped", "overlap"]) expect(checkLayout({ frames: [10], textRuns: 5, problems: [p(kind)] }).status).toBe("fail");
    expect(checkLayout({ frames: [], textRuns: 0, problems: [] }).status).toBe("fail");
    expect(checkLayout({ frames: [10, 20], textRuns: 5, problems: [], emptyFrames: [20] }).detail).toMatch(/no visible text in frame\(s\) 20/);
    expect(checkLayout({ frames: [10], textRuns: 5, problems: [p("occluded")] }).status).toBe("fail");
  });
  it("samples three points in every segment", () => {
    const program = { segments: [{ startFrame: 0, durationInFrames: 100 }, { startFrame: 100, durationInFrames: 10 }] } as unknown as Program;
    expect(layoutFrames(program)).toEqual([10, 50, 90, 101, 105, 109]);
  });
});

describe("runGate", () => {
  it("fails every check when there is no manifest, and never throws", async () => {
    const verdict = await runGate("/nonexistent/v.mp4", {
      inspectLayout: async () => ({ frames: [], textRuns: 0, problems: [] }),
      probe: async () => goodProbe,
      frameStats: async (_f, at) => ({ at, mean: 50, stddev: 30, peak: 220 }),
      audioStats: async () => ({ meanVolume: -20, maxVolume: -3, silences: [] }),
      now: () => new Date("2026-10-17T00:00:00Z"),
    });
    expect(verdict.verdict).toBe("fail");
    expect(verdict.failed).toEqual(expect.arrayContaining(["manifest", "speech", "layout", "ffprobe", "captions"]));
    expect(verdict.checks.find((c) => c.name === "manifest")?.detail).toMatch(/ENOENT|no such file/);
    expect(verdict.thresholds).toEqual(DEFAULT_THRESHOLDS);
  });
});
