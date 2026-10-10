import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { SceneTraceInput } from "../../src/api";

const here = path.dirname(fileURLToPath(import.meta.url));

/** The committed S1 illustrative trace, as raw JSON. */
export function s1TraceJson(): SceneTraceInput {
  return JSON.parse(
    readFileSync(path.join(here, "../../traces/illustrative/s1-plan-lifecycle.json"), "utf8"),
  ) as SceneTraceInput;
}

/** A minimal valid replay trace. */
export function replayTraceJson(): SceneTraceInput {
  return {
    trace_version: 1,
    meta: {
      mode: "replay",
      title: "A week of qontinui-design-tokens",
      source: { kind: "build-record", ref: "qontinui/qontinui-design-tokens", export_version: "1" },
      date_range: { from: "2026-09-01T00:00:00Z", to: "2026-09-07T23:59:59Z" },
    },
    entities: { plans: [{ id: "p1", title: "Motion tokens" }] },
    events: [
      { id: "e1", at: "2026-09-02T10:00:00Z", kind: "plan.drafted", plan: "p1" },
      { id: "e2", at: "2026-09-03T10:00:00Z", kind: "plan.vetted", plan: "p1" },
    ],
  };
}

export function clone<T>(value: T): T {
  return structuredClone(value);
}
