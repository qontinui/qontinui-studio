import { describe, expect, it } from "vitest";

import {
  BUILD_RECORD_EXPORT_VERSION,
  TRACE_VERSION,
  fromBuildRecord,
  parseTrace,
  validateTrace,
} from "../../src/api";
import { clone, replayTraceJson, s1TraceJson } from "./fixtures";

function errorsOf(input: unknown): string[] {
  const result = validateTrace(input);
  return result.ok ? [] : result.errors;
}

describe("scene trace schema", () => {
  it("accepts the committed S1 illustrative trace", () => {
    const result = validateTrace(s1TraceJson());
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.trace.trace_version).toBe(TRACE_VERSION);
      expect(result.trace.meta.mode).toBe("illustrative");
      expect(result.trace.events).toHaveLength(15);
    }
  });

  it("accepts a replay that states its date range and build-record source", () => {
    expect(validateTrace(replayTraceJson()).ok).toBe(true);
  });

  it("defaults omitted entity lists to empty", () => {
    const trace = parseTrace(replayTraceJson());
    expect(trace.entities.prs).toEqual([]);
    expect(trace.entities.sessions).toEqual([]);
    expect(trace.entities.sources).toEqual([]);
  });

  it("rejects a replay without a date range (plan D2)", () => {
    const json = clone(replayTraceJson());
    delete json.meta.date_range;
    expect(errorsOf(json)).toContain("meta.date_range: a replay trace must state its date range (plan D2)");
  });

  it("rejects a replay whose source is hand-authored", () => {
    const json = clone(replayTraceJson());
    json.meta.source = { kind: "hand-authored", ref: "me" };
    expect(errorsOf(json).join("\n")).toMatch(/a replay trace must come from a build record/);
  });

  it("rejects an illustrative trace that claims a build-record source", () => {
    const json = clone(s1TraceJson());
    json.meta.source = { kind: "build-record", ref: "x", export_version: "1" };
    expect(errorsOf(json).join("\n")).toMatch(/an illustrative trace is hand-authored/);
  });

  it("rejects an unknown trace_version", () => {
    const json = { ...clone(s1TraceJson()), trace_version: 2 };
    expect(errorsOf(json).join("\n")).toMatch(/^trace_version:/m);
  });

  it("rejects an unknown event kind", () => {
    const json = clone(s1TraceJson()) as unknown as { events: Record<string, unknown>[] };
    json.events[0] = { id: "x", at: "2026-09-14T09:00:00Z", kind: "plan.teleported", plan: "plan-retry-budget" };
    expect(errorsOf(json).join("\n")).toMatch(/^events\.0\.kind:/m);
  });

  it("rejects a timestamp without an offset", () => {
    const json = clone(s1TraceJson());
    (json.events[0] as { at: string }).at = "2026-09-14T09:00:00";
    expect(errorsOf(json).join("\n")).toMatch(/events\.0\.at: must be an ISO-8601 timestamp/);
  });

  it("rejects dangling entity references", () => {
    const json = clone(s1TraceJson());
    (json.events[0] as { plan: string }).plan = "nope";
    (json.events[6] as { pr: string }).pr = "pr-missing";
    const errors = errorsOf(json).join("\n");
    expect(errors).toMatch(/events\.0\.plan: unknown plan "nope"/);
    expect(errors).toMatch(/events\.6\.pr: unknown pr "pr-missing"/);
  });

  it("rejects duplicate ids", () => {
    const json = clone(s1TraceJson());
    (json.events[1] as { id: string }).id = "e01";
    expect(errorsOf(json).join("\n")).toMatch(/duplicate events id "e01"/);
  });

  it("rejects replay events outside the date range", () => {
    const json = clone(replayTraceJson());
    (json.events[1] as { at: string }).at = "2026-09-09T00:00:00Z";
    expect(errorsOf(json).join("\n")).toMatch(/events\.1\.at: event lies outside meta\.date_range/);
  });

  it("rejects unknown fields (strict)", () => {
    const json = clone(s1TraceJson()) as unknown as { meta: Record<string, unknown> };
    json.meta.real = true;
    expect(errorsOf(json).join("\n")).toMatch(/Unrecognized key/);
  });

  it("rejects a trace with no events", () => {
    const json = { ...clone(s1TraceJson()), events: [] };
    expect(validateTrace(json).ok).toBe(false);
  });

  it("parseTrace throws with every problem listed", () => {
    const json = clone(replayTraceJson());
    delete json.meta.date_range;
    expect(() => parseTrace(json)).toThrow(/invalid scene trace:\n {2}meta\.date_range/);
  });
});

describe("from-build-record adapter (stub until the sibling export ships)", () => {
  it("is unpinned and refuses to fabricate a replay", () => {
    expect(BUILD_RECORD_EXPORT_VERSION).toBeNull();
    expect(() => fromBuildRecord({ export_version: "1" }, { sourceRef: "x", title: "y" })).toThrow(
      "not implemented: needs the sibling plan's build-record export",
    );
  });
});
