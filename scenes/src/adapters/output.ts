/**
 * Output safety for every adapter: a render never deletes anything it did not
 * write, and never anything recursively.
 *
 * - Renders go into a fresh staging directory created beside the target, then
 *   are published (renamed) into place.
 * - A single-file target (mp4/webm/gif/png still) that already exists is
 *   refused unless `overwrite` is set; a directory there is always refused.
 * - A frame-sequence target directory must be absent, empty, or hold only
 *   `element-*.png` files from a previous run. With `overwrite`, exactly those
 *   files are replaced; anything else in it makes the render refuse.
 * - Staging cleanup unlinks only plain files inside the staging directory it
 *   created, then removes that (now empty) directory.
 */
import { lstat, mkdir, mkdtemp, readdir, rename, rmdir, unlink } from "node:fs/promises";
import path from "node:path";

/** The file names a frame-sequence render writes. */
export const SEQUENCE_FILE = /^element-\d+\.png$/;

export class OutputRefused extends Error {
  override readonly name = "OutputRefused";
}

async function statOrNull(p: string) {
  try {
    return await lstat(p);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  }
}

/** Refuse a single-file target that would destroy something. */
export async function checkFileTarget(file: string, overwrite: boolean): Promise<void> {
  const st = await statOrNull(file);
  if (!st) return;
  if (!st.isFile()) throw new OutputRefused(`refusing to write ${file}: it exists and is not a regular file`);
  if (!overwrite) throw new OutputRefused(`refusing to overwrite ${file}: it exists (pass --force to replace it)`);
}

/** Refuse a frame-sequence directory holding anything but a prior sequence. */
export async function checkSequenceTarget(dir: string, overwrite: boolean): Promise<string[]> {
  const st = await statOrNull(dir);
  if (!st) return [];
  if (!st.isDirectory()) throw new OutputRefused(`refusing to write a frame sequence to ${dir}: it is not a directory`);
  const entries = await readdir(dir, { withFileTypes: true });
  const foreign = entries.filter((e) => !(e.isFile() && SEQUENCE_FILE.test(e.name))).map((e) => e.name);
  if (foreign.length) {
    throw new OutputRefused(
      `refusing to write a frame sequence to ${dir}: it holds files this tool did not write (${foreign.slice(0, 5).join(", ")}${foreign.length > 5 ? ", …" : ""})`,
    );
  }
  const previous = entries.map((e) => e.name).sort();
  if (previous.length && !overwrite) {
    throw new OutputRefused(
      `refusing to write a frame sequence to ${dir}: it holds ${previous.length} element-*.png from a previous run (pass --force to replace exactly those)`,
    );
  }
  return previous;
}

/** Run `fn` with a fresh staging directory beside `target`; always clean it up. */
export async function withStaging<T>(target: string, fn: (stagingDir: string) => Promise<T>): Promise<T> {
  const parent = path.dirname(path.resolve(target));
  await mkdir(parent, { recursive: true });
  const staging = await mkdtemp(path.join(parent, ".studio-render-"));
  try {
    return await fn(staging);
  } finally {
    await cleanStaging(staging);
  }
}

async function cleanStaging(staging: string): Promise<void> {
  const entries = await readdir(staging, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.isFile()) await unlink(path.join(staging, entry.name));
  }
  try {
    await rmdir(staging);
  } catch {
    // Something we did not create a plain file for is in there; leave it.
    process.stderr.write(`note: left staging directory ${staging} (not empty)\n`);
  }
}

/** Move a staged single file into place (after re-checking the target). */
export async function publishFile(staged: string, target: string, overwrite: boolean): Promise<string> {
  await checkFileTarget(target, overwrite);
  await rename(staged, target);
  return target;
}

/** Move a staged frame sequence into `targetDir`, replacing only a prior sequence. */
export async function publishSequence(staging: string, targetDir: string, overwrite: boolean): Promise<string[]> {
  const previous = await checkSequenceTarget(targetDir, overwrite);
  await mkdir(targetDir, { recursive: true });
  for (const name of previous) await unlink(path.join(targetDir, name));
  const frames = (await readdir(staging)).filter((name) => SEQUENCE_FILE.test(name)).sort();
  const published: string[] = [];
  for (const name of frames) {
    const to = path.join(targetDir, name);
    await rename(path.join(staging, name), to);
    published.push(to);
  }
  return published;
}
