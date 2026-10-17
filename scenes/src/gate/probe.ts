/**
 * What the publish gate reads from a rendered file, through ffprobe and
 * ffmpeg (`STUDIO_FFPROBE` / `STUDIO_FFMPEG`, else on PATH). Each reader
 * returns plain data; the checks in ./checks.ts judge it.
 */
import { execFile } from "node:child_process";

export interface ProbeStream {
  codec_type?: string;
  codec_name?: string;
  width?: number;
  height?: number;
  pix_fmt?: string;
  r_frame_rate?: string;
  avg_frame_rate?: string;
  nb_frames?: string;
  /** With -count_packets: packets actually read (one per frame for video). Matroska/webm reports no nb_frames. */
  nb_read_packets?: string;
  duration?: string;
  sample_rate?: string;
  channels?: number;
}

export interface Probe {
  format?: { format_name?: string; duration?: string };
  streams?: ProbeStream[];
}

export interface FrameStats {
  /** Seconds into the video. */
  at: number;
  mean: number;
  stddev: number;
  /** The 99.9th-percentile luma (0..255): what the brightest content reaches, ignoring stray pixels. */
  peak: number;
}

export interface AudioStats {
  /** dBFS, from volumedetect; null when it reported nothing. */
  meanVolume: number | null;
  maxVolume: number | null;
  /** Silences at least the detector's minimum long: [start, end] in seconds (end null: to the end). */
  silences: { start: number; end: number | null }[];
}

export class ToolUnavailable extends Error {
  override readonly name = "ToolUnavailable";
}

const ffprobeBin = () => process.env.STUDIO_FFPROBE || "ffprobe";
const ffmpegBin = () => process.env.STUDIO_FFMPEG || "ffmpeg";

function run(bin: string, args: string[], encoding: "utf8" | "buffer" = "utf8"): Promise<{ stdout: Buffer | string; stderr: string }> {
  return new Promise((resolve, reject) => {
    execFile(
      bin,
      args,
      { encoding: encoding === "buffer" ? "buffer" : "utf8", maxBuffer: 64 * 1024 * 1024, timeout: 10 * 60_000 },
      (err, stdout, stderr) => {
        if (err && (err as NodeJS.ErrnoException).code === "ENOENT") {
          reject(new ToolUnavailable(`${bin} is not installed (set STUDIO_FFMPEG / STUDIO_FFPROBE, or put it on PATH)`));
          return;
        }
        if (err) {
          reject(new Error(`${bin} failed: ${String(stderr).slice(-400) || err.message}`));
          return;
        }
        resolve({ stdout, stderr: String(stderr) });
      },
    );
  });
}

/** `file:` + absolute path, so a name starting with `-` or holding `:` is never an option or protocol. */
const input = (file: string) => `file:${file}`;

export async function probe(file: string): Promise<Probe> {
  const { stdout } = await run(ffprobeBin(), ["-v", "error", "-count_packets", "-print_format", "json", "-show_format", "-show_streams", input(file)]);
  return JSON.parse(String(stdout)) as Probe;
}

/** Luma statistics of the frame at `at` seconds, downscaled to 320 px wide. */
export async function frameStats(file: string, at: number): Promise<FrameStats> {
  const { stdout } = await run(
    ffmpegBin(),
    ["-nostdin", "-v", "error", "-ss", at.toFixed(3), "-i", input(file), "-frames:v", "1", "-vf", "scale=320:-2,format=gray", "-f", "rawvideo", "pipe:1"],
    "buffer",
  );
  const pixels = stdout as Buffer;
  if (!pixels.length) throw new Error(`no frame decoded at ${at.toFixed(3)} s`);
  return { at, ...lumaStats(pixels) };
}

export function lumaStats(pixels: Uint8Array): Omit<FrameStats, "at"> {
  const histogram = new Array<number>(256).fill(0);
  let sum = 0;
  for (const p of pixels) {
    histogram[p] = (histogram[p] as number) + 1;
    sum += p;
  }
  const mean = sum / pixels.length;
  let variance = 0;
  for (let v = 0; v < 256; v++) variance += (histogram[v] as number) * (v - mean) ** 2;
  let seen = 0;
  let peak = 255;
  for (let v = 0; v < 256; v++) {
    seen += histogram[v] as number;
    if (seen >= pixels.length * 0.999) {
      peak = v;
      break;
    }
  }
  return { mean, stddev: Math.sqrt(variance / pixels.length), peak };
}

/** Volume and silences of the first audio stream (`silencedetect` at `noiseDb`, at least `minSilence` s). */
export async function audioStats(file: string, minSilence: number, noiseDb = -50): Promise<AudioStats> {
  const { stderr } = await run(ffmpegBin(), [
    "-nostdin",
    "-v",
    "info",
    "-nostats",
    "-i",
    input(file),
    "-map",
    "0:a:0",
    "-af",
    `silencedetect=noise=${noiseDb}dB:d=${minSilence},volumedetect`,
    "-f",
    "null",
    "-",
  ]);
  return parseAudioLog(stderr);
}

export function parseAudioLog(log: string): AudioStats {
  const num = (re: RegExp) => {
    const m = re.exec(log);
    return m ? Number(m[1]) : null;
  };
  const silences: AudioStats["silences"] = [];
  for (const line of log.split("\n")) {
    const start = /silence_start: (-?[\d.]+)/.exec(line);
    if (start) silences.push({ start: Math.max(0, Number(start[1])), end: null });
    const end = /silence_end: (-?[\d.]+)/.exec(line);
    if (end && silences.length) (silences[silences.length - 1] as { end: number | null }).end = Number(end[1]);
  }
  return { meanVolume: num(/mean_volume: (-?[\d.]+) dB/), maxVolume: num(/max_volume: (-?[\d.]+) dB/), silences };
}
