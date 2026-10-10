/**
 * The studio scene API — the only surface scenes may import (with
 * `src/primitives`). Nothing here imports an engine; see `engine.ts` for the
 * adapter seam (plan D7).
 */
export * from "./engine";
export * from "./format";
export * from "./layout";
export * from "./motion";
export * from "./scene";
export * from "./timeline";
export * from "./tokens";
export * from "./typography";
export * from "./trace/schema";
export * from "./trace/from-build-record";
export { assertRenderable, renderRefusal, REPLAY_REFUSAL } from "./trace/provenance";
export * from "./text-coverage";
