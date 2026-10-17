/**
 * The speech services the narration step talks to, over HTTP only.
 *
 * - **TTS: Kokoro-82M behind Kokoro-FastAPI** (`POST /dev/captioned_speech`),
 *   which returns the audio and its own per-word timestamps. Kokoro's Python
 *   stack links GPL code (phonemizer, espeak-ng), so it runs as a SEPARATE
 *   service and only its audio enters a render; the studio never links it
 *   (plan Phase 0, narration licence correction).
 * - **ASR: an OpenAI-compatible transcription endpoint** (Whisper:
 *   `POST /v1/audio/transcriptions`, `verbose_json`, word timestamps) — the
 *   independent check that what was spoken is what the script says.
 *
 * Both are configured by environment (or the CLI's flags). An API key, when
 * one is needed, is read from a FILE named by an environment variable, never
 * taken from the command line.
 */
import { readFile } from "node:fs/promises";

import type { HeardWord } from "./vendor/explainroo/align.js";

export interface SpeechConfig {
  ttsUrl: string | null;
  ttsVoice: string;
  ttsKeyFile: string | null;
  asrUrl: string | null;
  asrModel: string;
  asrKeyFile: string | null;
  timeoutMs: number;
}

export const DEFAULT_VOICE = "af_heart";
export const DEFAULT_ASR_MODEL = "whisper-1";

/** The configuration from the environment (see the narration README section). */
export function speechConfigFromEnv(env: NodeJS.ProcessEnv = process.env): SpeechConfig {
  const blank = (v: string | undefined) => (v === undefined || v.trim() === "" ? null : v.trim());
  const timeout = Number(env.STUDIO_SPEECH_TIMEOUT_MS ?? "");
  return {
    ttsUrl: blank(env.STUDIO_TTS_URL),
    ttsVoice: blank(env.STUDIO_TTS_VOICE) ?? DEFAULT_VOICE,
    ttsKeyFile: blank(env.STUDIO_TTS_API_KEY_FILE),
    asrUrl: blank(env.STUDIO_ASR_URL),
    asrModel: blank(env.STUDIO_ASR_MODEL) ?? DEFAULT_ASR_MODEL,
    asrKeyFile: blank(env.STUDIO_ASR_API_KEY_FILE),
    timeoutMs: Number.isFinite(timeout) && timeout > 0 ? timeout : 120_000,
  };
}

export class SpeechServiceError extends Error {
  override readonly name = "SpeechServiceError";
}

export interface SpokenChunk {
  /** WAV bytes. */
  wav: Uint8Array;
  /** The service's own word timestamps, seconds into this chunk. */
  words: HeardWord[];
}

export interface TtsClient {
  /** What spoke: shown in the timing table, e.g. "kokoro af_heart @ http://host:8880". */
  readonly describe: string;
  speak(text: string, options: { speed: number }): Promise<SpokenChunk>;
}

export interface Transcript {
  words: HeardWord[];
  text: string;
}

export interface AsrClient {
  readonly describe: string;
  transcribe(wav: Uint8Array): Promise<Transcript>;
}

export type Fetch = typeof fetch;

async function bearer(file: string | null): Promise<Record<string, string>> {
  if (!file) return {};
  let key: string;
  try {
    key = (await readFile(file, "utf8")).trim();
  } catch (err) {
    throw new SpeechServiceError(`cannot read the API key file ${file}: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!key) throw new SpeechServiceError(`the API key file ${file} is empty`);
  return { authorization: `Bearer ${key}` };
}

/**
 * The service's base URL, and the form of it that may be shown: no
 * user:password, no query, no fragment — those can carry secrets, and the
 * shown form lands in the timing table, on stdout and in errors.
 */
function base(url: string): { root: string; shown: string } {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new SpeechServiceError("a service URL is not a valid URL");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new SpeechServiceError(`${parsed.protocol} service URLs are not supported (http or https only)`);
  }
  if (parsed.username || parsed.password || parsed.search) {
    throw new SpeechServiceError(
      "a service URL may not carry credentials or a query; put a key in the file named by STUDIO_TTS_API_KEY_FILE / STUDIO_ASR_API_KEY_FILE",
    );
  }
  const root = `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
  return { root, shown: root };
}

/** Read a JSON body, telling a timeout apart from a body that is not JSON. */
async function jsonBody(response: Response, what: string): Promise<unknown> {
  try {
    return await response.json();
  } catch (err) {
    if (err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError")) {
      throw new SpeechServiceError(`${what} timed out while sending its answer`);
    }
    return null;
  }
}

async function call(fetchImpl: Fetch, what: string, url: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  let response: Response;
  try {
    response = await fetchImpl(url, { ...init, signal: AbortSignal.timeout(timeoutMs) });
  } catch (err) {
    throw new SpeechServiceError(`${what} ${url} did not answer: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) {
    const body = (await response.text().catch(() => "")).slice(0, 300);
    throw new SpeechServiceError(`${what} ${url} answered HTTP ${response.status}${body ? `: ${body}` : ""}`);
  }
  return response;
}

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Kokoro-FastAPI's captioned speech: `{audio: <base64>, audio_format, timestamps: [{word, start_time, end_time}]}`. */
export function kokoroTts(config: SpeechConfig, fetchImpl: Fetch = fetch): TtsClient {
  if (!config.ttsUrl) throw new SpeechServiceError("no TTS service configured (set STUDIO_TTS_URL to a Kokoro-FastAPI base URL)");
  const { root, shown } = base(config.ttsUrl);
  const url = `${root}/dev/captioned_speech`;
  return {
    describe: `kokoro ${config.ttsVoice} @ ${shown}`,
    async speak(text, { speed }) {
      const response = await call(
        fetchImpl,
        "the TTS service",
        url,
        {
          method: "POST",
          headers: { "content-type": "application/json", ...(await bearer(config.ttsKeyFile)) },
          body: JSON.stringify({
            model: "kokoro",
            input: text,
            voice: config.ttsVoice,
            response_format: "wav",
            speed,
            stream: false,
            return_timestamps: true,
          }),
        },
        config.timeoutMs,
      );
      const body = (await jsonBody(response, "the TTS service")) as {
        audio?: unknown;
        timestamps?: unknown;
      } | null;
      if (!body || typeof body.audio !== "string") throw new SpeechServiceError(`${url}: the answer carries no base64 audio`);
      const words: HeardWord[] = [];
      for (const t of Array.isArray(body.timestamps) ? body.timestamps : []) {
        const w = t as { word?: unknown; start_time?: unknown; end_time?: unknown };
        if (typeof w.word === "string" && finite(w.start_time) && finite(w.end_time)) {
          words.push({ text: w.word, start: w.start_time, end: w.end_time });
        }
      }
      return { wav: Buffer.from(body.audio, "base64"), words };
    },
  };
}

/**
 * OpenAI-compatible transcription (Whisper). Word timestamps when the server
 * gives them; otherwise the transcript's words without times (the check needs
 * only the words).
 */
export function whisperAsr(config: SpeechConfig, fetchImpl: Fetch = fetch): AsrClient {
  if (!config.asrUrl) throw new SpeechServiceError("no ASR service configured (set STUDIO_ASR_URL to an OpenAI-compatible base URL)");
  const { root, shown } = base(config.asrUrl);
  const url = `${root}/v1/audio/transcriptions`;
  return {
    describe: `${config.asrModel} @ ${shown}`,
    async transcribe(wav) {
      const form = new FormData();
      form.append("file", new Blob([Uint8Array.from(wav)], { type: "audio/wav" }), "clip.wav");
      form.append("model", config.asrModel);
      form.append("response_format", "verbose_json");
      form.append("timestamp_granularities[]", "word");
      form.append("language", "en");
      const response = await call(
        fetchImpl,
        "the ASR service",
        url,
        { method: "POST", headers: await bearer(config.asrKeyFile), body: form },
        config.timeoutMs,
      );
      const body = (await jsonBody(response, "the ASR service")) as { text?: unknown; words?: unknown } | null;
      if (!body || typeof body.text !== "string") throw new SpeechServiceError(`${url}: the answer carries no transcript text`);
      const words: HeardWord[] = [];
      for (const w of Array.isArray(body.words) ? body.words : []) {
        const word = w as { word?: unknown; start?: unknown; end?: unknown };
        if (typeof word.word === "string" && finite(word.start) && finite(word.end)) {
          words.push({ text: word.word, start: word.start, end: word.end });
        }
      }
      if (!words.length) {
        for (const text of body.text.split(/\s+/).filter(Boolean)) words.push({ text, start: 0, end: 0 });
      }
      return { words, text: body.text };
    },
  };
}
