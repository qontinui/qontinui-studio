/**
 * Minimal 16-bit PCM WAV reading and writing for narration clips: what the
 * TTS service returns, what the narration step concatenates (with silences
 * between sentences) and what a composition plays.
 */

export interface PcmAudio {
  sampleRate: number;
  channels: number;
  /** Interleaved 16-bit samples. */
  samples: Int16Array;
}

export class WavError extends Error {
  override readonly name = "WavError";
}

/** Parse a RIFF/WAVE file holding 16-bit PCM. Anything else is refused, naming what it is. */
export function readWav(bytes: Uint8Array): PcmAudio {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const tag = (offset: number) => String.fromCharCode(...bytes.subarray(offset, offset + 4));
  if (bytes.byteLength < 12 || tag(0) !== "RIFF" || tag(8) !== "WAVE") throw new WavError("not a RIFF/WAVE file");
  let format: { audioFormat: number; channels: number; sampleRate: number; bits: number; subFormat: number | null } | null = null;
  let offset = 12;
  while (offset + 8 <= bytes.byteLength) {
    const id = tag(offset);
    const declared = view.getUint32(offset + 4, true);
    const body = offset + 8;
    // A streamed WAV may declare 0 or 0xFFFFFFFF for its data chunk: take the rest.
    const size = id === "data" && (declared === 0 || body + declared > bytes.byteLength) ? bytes.byteLength - body : declared;
    if (id === "fmt ") {
      if (size < 16) throw new WavError("fmt chunk too short");
      format = {
        audioFormat: view.getUint16(body, true),
        channels: view.getUint16(body + 2, true),
        sampleRate: view.getUint32(body + 4, true),
        bits: view.getUint16(body + 14, true),
        // WAVE_FORMAT_EXTENSIBLE carries the real format in its sub-format GUID.
        subFormat: size >= 40 ? view.getUint16(body + 24, true) : null,
      };
    } else if (id === "data") {
      if (!format) throw new WavError("data chunk before fmt chunk");
      // 1 = PCM; 0xFFFE = WAVE_FORMAT_EXTENSIBLE whose sub-format is PCM (1).
      const pcm = format.audioFormat === 1 || (format.audioFormat === 0xfffe && format.subFormat === 1);
      if (!pcm || format.bits !== 16) {
        throw new WavError(`only 16-bit PCM is supported (format ${format.audioFormat}, ${format.bits} bits)`);
      }
      if (format.channels < 1 || format.sampleRate < 1) throw new WavError("invalid channel count or sample rate");
      // Whole sample frames only, so channels never shift.
      const count = Math.floor(size / (2 * format.channels)) * format.channels;
      const samples = new Int16Array(count);
      for (let i = 0; i < count; i++) samples[i] = view.getInt16(body + i * 2, true);
      return { sampleRate: format.sampleRate, channels: format.channels, samples };
    }
    offset = body + size + (size % 2);
  }
  throw new WavError("no data chunk");
}

export function writeWav(audio: PcmAudio): Buffer {
  const data = Buffer.alloc(audio.samples.length * 2);
  for (let i = 0; i < audio.samples.length; i++) data.writeInt16LE(audio.samples[i] as number, i * 2);
  const header = Buffer.alloc(44);
  header.write("RIFF", 0, "ascii");
  header.writeUInt32LE(36 + data.length, 4);
  header.write("WAVEfmt ", 8, "ascii");
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(audio.channels, 22);
  header.writeUInt32LE(audio.sampleRate, 24);
  header.writeUInt32LE(audio.sampleRate * audio.channels * 2, 28);
  header.writeUInt16LE(audio.channels * 2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36, "ascii");
  header.writeUInt32LE(data.length, 40);
  return Buffer.concat([header, data]);
}

export function durationSeconds(audio: PcmAudio): number {
  return audio.samples.length / audio.channels / audio.sampleRate;
}

/** Silence of `seconds` in `like`'s format. */
export function silence(like: Pick<PcmAudio, "sampleRate" | "channels">, seconds: number): PcmAudio {
  const frames = Math.max(0, Math.round(seconds * like.sampleRate));
  return { sampleRate: like.sampleRate, channels: like.channels, samples: new Int16Array(frames * like.channels) };
}

/** Clips joined end to end; every clip must share one format. */
export function concat(clips: readonly PcmAudio[]): PcmAudio {
  const first = clips[0];
  if (!first) throw new WavError("nothing to join");
  for (const clip of clips) {
    if (clip.sampleRate !== first.sampleRate || clip.channels !== first.channels) {
      throw new WavError(
        `clips differ in format (${clip.sampleRate} Hz x${clip.channels} vs ${first.sampleRate} Hz x${first.channels})`,
      );
    }
  }
  const samples = new Int16Array(clips.reduce((n, c) => n + c.samples.length, 0));
  let at = 0;
  for (const clip of clips) {
    samples.set(clip.samples, at);
    at += clip.samples.length;
  }
  return { sampleRate: first.sampleRate, channels: first.channels, samples };
}
