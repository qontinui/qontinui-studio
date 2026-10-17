/**
 * Narration (plan Phase 5): the vendored explainroo alignment and cue
 * grammar, the WAV helpers, the Kokoro-FastAPI and Whisper clients against
 * shape fixtures, the speak -> re-transcribe -> align loop, and the CLI.
 * No network: a fake fetch, and a loopback server the test itself starts.
 */
import { spawn } from "node:child_process";
import { createServer, type Server } from "node:http";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { defineComposition } from "../../src/api/composition";
import { validateTiming } from "../../src/api/narration-timing";
import { narrate, NarrationError } from "../../src/narration/narrate";
import {
  SpeechServiceError,
  kokoroTts,
  speechConfigFromEnv,
  whisperAsr,
  type AsrClient,
  type Fetch,
  type TtsClient,
} from "../../src/narration/services";
import { alignWords, expand, yearWords } from "../../src/narration/vendor/explainroo/align.js";
import { parseScript, resolveMarks, speechChunks, tokenizeNarration } from "../../src/narration/vendor/explainroo/script.js";
import { concat, durationSeconds, readWav, silence, writeWav } from "../../src/narration/wav";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const FIXTURES = path.join(ROOT, "tests/fixtures/narration");
const BIN = path.join(ROOT, "bin/studio-scenes.mjs");

type KokoroFixture = { audio: string; audio_format: string; timestamps: unknown[]; duration_seconds: number };
const kokoro = JSON.parse(readFileSync(path.join(FIXTURES, "kokoro-captioned-speech.json"), "utf8")) as Record<string, KokoroFixture>;
const whisper = JSON.parse(readFileSync(path.join(FIXTURES, "whisper-verbose.json"), "utf8")) as Record<string, unknown>;

/** A 24 kHz mono tone of `seconds`, as Kokoro returns WAV. */
function tone(seconds: number): Buffer {
  const rate = 24_000;
  const samples = new Int16Array(Math.round(seconds * rate));
  for (let i = 0; i < samples.length; i++) samples[i] = Math.round(Math.sin((2 * Math.PI * 200 * i) / rate) * 4000);
  return writeWav({ sampleRate: rate, channels: 1, samples });
}

/** The Kokoro answer for `input`, with a generated clip of the fixture's length. */
function kokoroAnswer(input: string) {
  const fixture = kokoro[input];
  if (!fixture) return null;
  const { duration_seconds, ...rest } = fixture;
  return { ...rest, audio: tone(duration_seconds).toString("base64") };
}

const SCRIPT = `# Test
## lifecycle
A plan ships, and twenty checks pass. [#e14] The {SQL|sequel} job lands.
`;

const composition = defineComposition({
  id: "test-comp",
  title: "t",
  description: "t",
  segments: [
    { kind: "card", id: "open", title: "t", seconds: 1 },
    { kind: "scene", id: "lifecycle", scene: "s1-plan-lifecycle" },
  ],
});

// ---------------------------------------------------------------------------

describe("vendored explainroo alignment (upstream's English tests, ported)", () => {
  const heard = (list: [string, number, number][]) => list.map(([text, start, end]) => ({ text, start, end }));

  it("aligns exact words", () => {
    const r = alignWords([{ spoken: "Hello" }, { spoken: "world." }], heard([["Hello", 0, 0.4], ["world.", 0.5, 0.9]]), 1);
    expect(r.matchRate).toBe(1);
    expect(r.words.map((w) => [w.start, w.end])).toEqual([[0, 0.4], [0.5, 0.9]]);
  });
  it("matches number words to digits, amounts and percent", () => {
    expect(alignWords([{ spoken: "about" }, { spoken: "twenty" }, { spoken: "milliseconds." }], heard([["about", 0, 0.3], ["20", 0.3, 0.6], ["milliseconds.", 0.6, 1.2]]), 1.3).matchRate).toBe(1);
    const r = alignWords(
      [{ spoken: "one thousand and fifty dollars." }, { spoken: "at" }, { spoken: "five percent" }],
      heard([["$1,050.", 0, 1], ["at", 1, 1.2], ["5%", 1.2, 1.6]]),
      2,
    );
    expect(r.matchRate).toBe(1);
  });
  it("matches a spelled-out group to one heard word, and dotted names", () => {
    const r = alignWords([{ spoken: "The" }, { spoken: "D N S" }, { spoken: "lookup" }], heard([["The", 0, 0.2], ["DNS", 0.2, 0.7], ["lookup", 0.8, 1.2]]), 1.3);
    expect(r.words[1]).toMatchObject({ matched: true, start: 0.2, end: 0.7 });
    expect(alignWords([{ spoken: "one dot one dot one dot one" }], heard([["1.1.1.1", 0, 1.4]]), 1.5).matchRate).toBe(1);
  });
  it("confirms a word Whisper writes in its shown form", () => {
    const r = alignWords([{ spoken: "The" }, { spoken: "sequel", display: "SQL" }, { spoken: "query." }], heard([["The", 0, 0.2], ["SQL", 0.2, 0.7], ["query.", 0.7, 1.1]]), 1.2);
    expect(r.matchRate).toBe(1);
    expect(alignWords([{ spoken: "sequel", display: "SQL" }], heard([["table", 0, 0.5]]), 0.6).words[0]!.matched).toBe(false);
  });
  it("interpolates missing words and keeps times monotonic", () => {
    const r = alignWords([{ spoken: "one" }, { spoken: "xyzzy" }, { spoken: "three" }], heard([["one", 0, 0.4], ["three", 1.0, 1.4]]), 1.5);
    expect(r.words[1]!.matched).toBe(false);
    expect(r.words[1]!.start).toBeGreaterThanOrEqual(0.4);
    expect(r.words[1]!.end).toBeLessThanOrEqual(1.0);
  });
  it("reads four-digit numbers like years", () => {
    expect(yearWords(1582)).toEqual(["fifteen", "eighty", "two"]);
    expect(yearWords(2000)).toBeNull();
    expect(expand("1,582")).toHaveLength(6);
  });
});

describe("the cue grammar", () => {
  it("parses marks, pauses and {shown|spoken}", () => {
    const units = tokenizeNarration("The {SQL|sequel} job [#e14] lands. [pause 0.6] Done.");
    expect(units).toEqual([
      { type: "word", display: "The", spoken: "The", space: true },
      { type: "word", display: "SQL", spoken: "sequel", space: true },
      { type: "word", display: "job", spoken: "job", space: true },
      { type: "mark", name: "e14" },
      { type: "word", display: "lands.", spoken: "lands.", space: true },
      { type: "pause", sec: 0.6 },
      { type: "word", display: "Done.", spoken: "Done.", space: true },
    ]);
    const chunks = speechChunks(units);
    expect(chunks.map((c) => c.text)).toEqual(["The sequel job lands.", "Done."]);
    expect(chunks[1]!.gapBefore).toBeCloseTo(0.9);
  });
  it("resolves a mark to the next word's start", () => {
    const units = tokenizeNarration("a [#m] b");
    expect(resolveMarks(units, [{ start: 0, end: 0.2 }, null, { start: 0.5, end: 0.7 }])).toEqual({ m: 0.5 });
  });
  it("refuses a malformed cue, naming the line", () => {
    expect(() => parseScript("## s\nThe {SQL} job.")).toThrow(/pronunciation overrides look like \{shown\|spoken\}/);
    expect(() => parseScript("## s\nwait [pause 99]")).toThrow(/pauses look like/);
  });
});

describe("WAV", () => {
  it("round-trips, joins and measures", () => {
    const a = readWav(tone(0.5));
    expect(a).toMatchObject({ sampleRate: 24_000, channels: 1 });
    expect(durationSeconds(a)).toBeCloseTo(0.5);
    const joined = concat([a, silence(a, 0.25), a]);
    expect(durationSeconds(joined)).toBeCloseTo(1.25);
    expect(readWav(writeWav(joined)).samples).toEqual(joined.samples);
  });
  it("reads a streamed data size, odd-chunk padding and a LIST chunk before fmt", () => {
    const pcm = tone(0.1);
    const fmt = pcm.subarray(12, 36);
    const data = pcm.subarray(36);
    const list = Buffer.concat([Buffer.from("LIST"), Buffer.from([3, 0, 0, 0]), Buffer.from("abc"), Buffer.from([0])]);
    for (const declared of [0, 0xffffffff]) {
      const d = Buffer.from(data);
      d.writeUInt32LE(declared, 4);
      const file = Buffer.concat([pcm.subarray(0, 12), list, fmt, d]);
      const audio = readWav(file);
      expect(audio.samples.length).toBe(2400);
      expect(audio.samples).toEqual(readWav(pcm).samples);
    }
  });

  it("keeps whole sample frames of a stereo clip", () => {
    const stereo = writeWav({ sampleRate: 8000, channels: 2, samples: new Int16Array([1, 2, 3, 4]) });
    const cut = Buffer.concat([stereo, Buffer.from([9, 9])]);
    cut.writeUInt32LE(4 * 2 + 2, 40);
    expect([...readWav(cut).samples]).toEqual([1, 2, 3, 4]);
  });

  it("refuses what is not 16-bit PCM", () => {
    expect(() => readWav(Buffer.from("not a wav file at all"))).toThrow(/RIFF/);
    const float = tone(0.1);
    float.writeUInt16LE(3, 20); // IEEE float
    float.writeUInt16LE(32, 34);
    expect(() => readWav(float)).toThrow(/only 16-bit PCM/);
  });
});

// ---------------------------------------------------------------------------

/** A fetch that answers like Kokoro-FastAPI and Whisper from the fixtures, recording each request. */
function fakeFetch(transcript: "good" | "garbled") {
  const requests: { url: string; body: unknown; headers: Record<string, string> }[] = [];
  const impl: Fetch = async (input, init) => {
    const url = String(input);
    const headers = Object.fromEntries(new Headers(init?.headers).entries());
    if (url.endsWith("/dev/captioned_speech")) {
      const body = JSON.parse(String(init?.body)) as { input: string };
      requests.push({ url, body, headers });
      const answer = kokoroAnswer(body.input);
      return answer ? Response.json(answer) : new Response("unknown sentence", { status: 400 });
    }
    if (url.endsWith("/v1/audio/transcriptions")) {
      const form = init?.body as FormData;
      requests.push({ url, body: Object.fromEntries([...form.entries()].map(([k, v]) => [k, typeof v === "string" ? v : "<file>"])), headers });
      return Response.json(whisper[transcript]);
    }
    return new Response("not found", { status: 404 });
  };
  return { impl, requests };
}

const config = { ...speechConfigFromEnv({}), ttsUrl: "http://tts.test:8880/", asrUrl: "http://asr.test:9000" };

describe("the service clients", () => {
  it("asks Kokoro-FastAPI for captioned WAV and reads its timestamps", async () => {
    const { impl, requests } = fakeFetch("good");
    const tts = kokoroTts(config, impl);
    expect(tts.describe).toBe("kokoro af_heart @ http://tts.test:8880");
    const spoken = await tts.speak("The sequel job lands.", { speed: 1 });
    expect(requests[0]).toMatchObject({
      url: "http://tts.test:8880/dev/captioned_speech",
      body: { model: "kokoro", input: "The sequel job lands.", voice: "af_heart", response_format: "wav", stream: false, return_timestamps: true },
    });
    expect(spoken.words[1]).toEqual({ text: "sequel", start: 0.2, end: 0.6 });
    expect(durationSeconds(readWav(spoken.wav))).toBeCloseTo(1.5);
  });

  it("clamps a word Kokoro starts before the clip's start to zero", async () => {
    // Kokoro-FastAPI v0.9.0 answers like the first word for some sentences (observed
    // 2026-10-17). "odd" only exercises the clamp: the timing table would still
    // refuse it, since it starts before the word before it.
    const early: Fetch = async () =>
      Response.json({
        audio: Buffer.from(tone(0.5)).toString("base64"),
        audio_format: "audio/wav",
        timestamps: [
          { word: "When", start_time: -0.019, end_time: 0.093 },
          { word: "red", start_time: 0.093, end_time: 0.2 },
          { word: "odd", start_time: -0.05, end_time: -0.01 },
        ],
      });
    const spoken = await kokoroTts(config, early).speak("When red odd.", { speed: 1 });
    expect(spoken.words).toEqual([
      { text: "When", start: 0, end: 0.093 },
      { text: "red", start: 0.093, end: 0.2 },
      { text: "odd", start: 0, end: 0 },
    ]);
  });

  it("asks Whisper for verbose JSON with word timestamps", async () => {
    const { impl, requests } = fakeFetch("good");
    const transcript = await whisperAsr(config, impl).transcribe(tone(0.2));
    expect(requests[0]).toMatchObject({
      url: "http://asr.test:9000/v1/audio/transcriptions",
      body: { model: "whisper-1", response_format: "verbose_json", "timestamp_granularities[]": "word", file: "<file>" },
    });
    expect(transcript.words).toHaveLength(11);
  });

  it("reads an API key from a file, never from arguments", async () => {
    const keyFile = path.join(mkdtempSync(path.join(tmpdir(), "studio-key-")), "key");
    writeFileSync(keyFile, "sk-test\n");
    const { impl, requests } = fakeFetch("good");
    await kokoroTts({ ...config, ttsKeyFile: keyFile }, impl).speak("The sequel job lands.", { speed: 1 });
    expect(requests[0]!.headers.authorization).toBe("Bearer sk-test");
  });

  it("refuses credentials or a query in a service URL, and never shows them", () => {
    expect(() => kokoroTts({ ...config, ttsUrl: "http://user:pw@tts.test:8880" })).toThrow(/may not carry credentials/);
    expect(() => whisperAsr({ ...config, asrUrl: "http://asr.test/?key=secret" })).toThrow(/may not carry credentials/);
    try {
      whisperAsr({ ...config, asrUrl: "http://asr.test/?key=secret" });
    } catch (err) {
      expect(String(err)).not.toContain("secret");
    }
  });

  it("types an unreadable key file as a service error", async () => {
    const { impl } = fakeFetch("good");
    await expect(kokoroTts({ ...config, ttsKeyFile: "/nonexistent/key" }, impl).speak("The sequel job lands.", { speed: 1 })).rejects.toThrow(SpeechServiceError);
  });

  it("types every failure as a service error", async () => {
    expect(() => kokoroTts({ ...config, ttsUrl: null })).toThrow(/set STUDIO_TTS_URL/);
    expect(() => kokoroTts({ ...config, ttsUrl: "file:///etc/passwd" })).toThrow(/http or https only/);
    const failing: Fetch = async () => new Response("boom", { status: 503 });
    await expect(kokoroTts(config, failing).speak("x", { speed: 1 })).rejects.toThrow(SpeechServiceError);
    const refused: Fetch = async () => {
      throw new TypeError("fetch failed");
    };
    await expect(whisperAsr(config, refused).transcribe(tone(0.1))).rejects.toThrow(/did not answer: fetch failed/);
  });
});

describe("narrate: speak -> re-transcribe -> align", () => {
  const clients = (transcript: "good" | "garbled") => {
    const { impl } = fakeFetch(transcript);
    return { tts: kokoroTts(config, impl) as TtsClient, asr: whisperAsr(config, impl) as AsrClient };
  };

  it("writes a valid timing table: words, marks and a measured speech match", async () => {
    const { tts, asr } = clients("good");
    const result = await narrate({ definition: composition, script: SCRIPT, tts, asr });
    expect(validateTiming(result.timing).ok).toBe(true);
    const segment = result.timing.segments.lifecycle!;
    // Two sentences of 2.3 s and 1.5 s, 0.3 s apart.
    expect(segment.duration_seconds).toBeCloseTo(4.1, 2);
    expect(segment.words.map((w) => w.text)).toEqual(["A", "plan", "ships,", "and", "twenty", "checks", "pass.", "The", "SQL", "job", "lands."]);
    expect(segment.words.every((w) => w.matched)).toBe(true);
    // The mark sits on "The", in the second sentence.
    expect(segment.marks.e14).toBeCloseTo(2.3 + 0.3 + 0.05, 2);
    expect(segment.speech).toEqual({ status: "measured", by: "whisper-1 @ http://asr.test:9000", match: 1, unheard: [] });
    expect(result.segments[0]!.narration.audio_sha256).toMatch(/^[0-9a-f]{64}$/);
  });

  it("measures a garbled clip below the gate's 0.9 and names the unheard words", async () => {
    const { tts, asr } = clients("garbled");
    const result = await narrate({ definition: composition, script: SCRIPT, tts, asr });
    const speech = result.timing.segments.lifecycle!.speech!;
    expect(speech.status).toBe("measured");
    if (speech.status !== "measured") return;
    expect(speech.match).toBeLessThan(0.9);
    // "plant" for "plan" is within the aligner's fuzzy tolerance (upstream's `similar`).
    expect(speech.unheard).toEqual(["twenty", "pass.", "SQL", "lands."]);
  });

  it("records the check as unavailable when there is no recogniser (never a pass)", async () => {
    const { tts } = clients("good");
    const result = await narrate({ definition: composition, script: SCRIPT, tts, asr: null, asrUnavailableReason: "STUDIO_ASR_URL is not set" });
    expect(result.timing.segments.lifecycle!.speech).toEqual({ status: "unavailable", reason: "STUDIO_ASR_URL is not set" });
  });

  it("keeps a mark after the last word inside the clip, so the table stays valid", async () => {
    const { tts, asr } = clients("good");
    const result = await narrate({ definition: composition, script: "## lifecycle\nThe {SQL|sequel} job lands. [#end]\n", tts, asr });
    const segment = result.timing.segments.lifecycle!;
    expect(segment.marks.end).toBeLessThanOrEqual(segment.duration_seconds);
    expect(validateTiming(result.timing).ok).toBe(true);
  });

  it("refuses a mark name the timing table cannot hold, a repeated mark, and a section without words", async () => {
    const { tts, asr } = clients("good");
    await expect(narrate({ definition: composition, script: "## lifecycle\n[#_x] The {SQL|sequel} job lands.", tts, asr })).rejects.toThrow(/must start with a letter or digit/);
    await expect(narrate({ definition: composition, script: "## lifecycle\n[#a] The {SQL|sequel} job [#a] lands.", tts, asr })).rejects.toThrow(/used twice/);
    await expect(narrate({ definition: composition, script: "## lifecycle\n[#a]\n", tts, asr })).rejects.toThrow(/no words to speak/);
  });

  it("types audio that is not PCM WAV as a service failure", async () => {
    const bad: TtsClient = { describe: "bad", speak: async () => ({ wav: Buffer.from("not audio"), words: [] }) };
    await expect(narrate({ definition: composition, script: SCRIPT, tts: bad, asr: null })).rejects.toThrow(SpeechServiceError);
  });

  it("refuses a script section naming no segment of the composition", async () => {
    const { tts, asr } = clients("good");
    await expect(narrate({ definition: composition, script: "## ghost\nHello.", tts, asr })).rejects.toThrow(NarrationError);
  });
});

// ---------------------------------------------------------------------------

describe("studio-scenes narrate (CLI)", () => {
  let server: Server;
  let url: string;
  beforeAll(async () => {
    server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (c: Buffer) => chunks.push(c));
      req.on("end", () => {
        if (req.url === "/dev/captioned_speech") {
          const answer = kokoroAnswer((JSON.parse(Buffer.concat(chunks).toString("utf8")) as { input: string }).input);
          res.writeHead(answer ? 200 : 400, { "content-type": "application/json" });
          res.end(JSON.stringify(answer ?? { error: "unknown sentence" }));
        } else if (req.url === "/v1/audio/transcriptions") {
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify(whisper.good));
        } else {
          res.writeHead(404).end();
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    url = `http://127.0.0.1:${typeof address === "object" && address ? address.port : 0}`;
  });
  afterAll(() => new Promise<void>((resolve) => server.close(() => resolve())));

  /** Async, so the loopback server in this process can answer. */
  function cli(env: Record<string, string>, ...args: string[]) {
    return new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve) => {
      const child = spawn(process.execPath, [BIN, ...args], {
        cwd: ROOT,
        env: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "", ...env },
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
      child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
      child.on("close", (status) => resolve({ status, stdout, stderr }));
    });
  }

  const scriptFile = () => {
    const file = path.join(mkdtempSync(path.join(tmpdir(), "studio-script-")), "teaser.md");
    writeFileSync(file, "## lifecycle\nA plan ships, and twenty checks pass. [#e14] The {SQL|sequel} job lands.\n");
    return file;
  };

  it("exits 6 when no TTS service is configured, writing nothing", async () => {
    const out = path.join(mkdtempSync(path.join(tmpdir(), "studio-narr-")), "voice");
    const r = await cli({}, "narrate", "teaser", "--script", scriptFile(), "--out", out);
    expect(r.status).toBe(6);
    expect(r.stderr).toMatch(/no TTS service configured/);
    expect(() => readdirSync(out)).toThrow();
  });

  it("exits 6 when the key file cannot be read", async () => {
    const out = path.join(mkdtempSync(path.join(tmpdir(), "studio-narr-")), "voice");
    const r = await cli({ STUDIO_TTS_URL: url, STUDIO_TTS_API_KEY_FILE: "/nonexistent/key" }, "narrate", "teaser", "--script", scriptFile(), "--out", out);
    expect(r.status).toBe(6);
    expect(r.stderr).toMatch(/cannot read the API key file/);
  });

  it("voices the script and writes the clip and the timing table", async () => {
    const out = path.join(mkdtempSync(path.join(tmpdir(), "studio-narr-")), "voice");
    const r = await cli({ STUDIO_TTS_URL: url, STUDIO_ASR_URL: url }, "narrate", "teaser", "--script", scriptFile(), "--out", out);
    expect(r.stderr).toBe("");
    expect(r.status).toBe(0);
    expect(readdirSync(out).sort()).toEqual(["lifecycle.wav", "teaser.timing.json"]);
    const table = validateTiming(JSON.parse(readFileSync(path.join(out, "teaser.timing.json"), "utf8")));
    expect(table.ok).toBe(true);
    expect(r.stdout).toMatch(/lifecycle: 4\.10 s, 1 marks; speech match 1\.000/);
    // A second run needs --force; a foreign file is never overwritten.
    expect((await cli({ STUDIO_TTS_URL: url }, "narrate", "teaser", "--script", scriptFile(), "--out", out)).stderr).toMatch(/pass --force/);
    const forced = await cli({ STUDIO_TTS_URL: url, STUDIO_ASR_URL: url }, "narrate", "teaser", "--script", scriptFile(), "--out", out, "--force");
    expect(forced.status).toBe(0);
    expect(readdirSync(out).sort()).toEqual(["lifecycle.wav", "teaser.timing.json"]);
    writeFileSync(path.join(out, "notes.txt"), "mine");
    expect((await cli({ STUDIO_TTS_URL: url }, "narrate", "teaser", "--script", scriptFile(), "--out", out, "--force")).stderr).toMatch(/did not write \(notes\.txt\)/);
  });
});
