/** Helpers shared by the CLI's commands. */
import { readFile } from "node:fs/promises";
import { parseArgs } from "node:util";

import type { EngineAdapter } from "../api/engine";
import { validateTrace, type SceneTrace } from "../api/trace/schema";
import { getScene, scenes } from "../scenes";
import { UsageError } from "./interruptible";

export const ENGINES = ["remotion", "hyperframes"] as const;
export type Engine = (typeof ENGINES)[number];

export function requireScene(id: string) {
  if (!scenes[id]) {
    throw new UsageError(`unknown scene "${id}" (known: ${Object.keys(scenes).join(", ")})`);
  }
  return getScene(id);
}

export async function readJson(file: string, what: string): Promise<unknown> {
  try {
    return JSON.parse(await readFile(file, "utf8"));
  } catch (err) {
    throw new UsageError(`cannot read ${what} ${file}: ${err instanceof Error ? err.message : String(err)}`);
  }
}

export async function loadTrace(file: string): Promise<SceneTrace> {
  const result = validateTrace(await readJson(file, "trace"));
  if (!result.ok) {
    throw new UsageError(`invalid trace ${file}:\n  ${result.errors.join("\n  ")}`);
  }
  return result.trace;
}

/** parseArgs with its errors (unknown option, missing value) as usage errors. */
export function parse<T extends Parameters<typeof parseArgs>[0]>(config: T) {
  try {
    return parseArgs(config);
  } catch (err) {
    throw new UsageError(err instanceof Error ? err.message : String(err));
  }
}

export function intOption(name: string, value: string | undefined): number | undefined {
  if (value === undefined) return undefined;
  if (!/^\d+$/.test(value)) throw new UsageError(`--${name} must be a non-negative integer`);
  return Number(value);
}

/** The adapter for `engine`, loaded only when used (lint: dynamic import only). */
export async function loadAdapter(engine: Engine): Promise<EngineAdapter> {
  if (engine === "hyperframes") {
    const { HyperFramesAdapter } = await import("../adapters/hyperframes/render");
    return new HyperFramesAdapter();
  }
  const { RemotionAdapter } = await import("../adapters/remotion/render");
  // The CLI handles SIGINT itself (runInterruptible), so Remotion's exit-at-once
  // listener is removed and an interrupted render cleans up.
  return new RemotionAdapter({ interrupts: "caller" });
}

