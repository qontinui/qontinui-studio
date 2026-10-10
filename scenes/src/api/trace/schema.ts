/**
 * The scene trace — the studio-owned, versioned input every scene animates.
 *
 * A trace is DATA, never shaped for a particular scene: scenes declare which
 * event kinds and entities they need (`SceneDefinition.requires`) and ignore
 * the rest. Replay traces are produced from the factory's build-record export
 * by `fromBuildRecord` (pinned to that export's version); illustrative traces
 * are hand-authored. The export is never reshaped to suit a scene (plan D6).
 *
 * Bump `TRACE_VERSION` on any breaking change to this schema.
 */
import { z } from "zod";

import { describeCharacter, uncoveredCharacters } from "../text-coverage";

export const TRACE_VERSION = 1 as const;

const isoTimestamp = z
  .string()
  .datetime({ offset: true, message: "must be an ISO-8601 timestamp with a UTC offset" });

const id = z.string().min(1).max(128).regex(/^[A-Za-z0-9._:/#-]+$/, "ids use [A-Za-z0-9._:/#-]");

// ---------------------------------------------------------------------------
// Entities
// ---------------------------------------------------------------------------

export const planEntity = z
  .object({
    id,
    title: z.string().min(1).max(160),
  })
  .strict();

export const prEntity = z
  .object({
    id,
    plan: id,
    repo: z.string().min(1).max(100),
    number: z.number().int().positive(),
    title: z.string().min(1).max(160),
  })
  .strict();

export const sessionEntity = z
  .object({
    id,
    /** Display name only — never a machine name or an account (sibling D3). */
    label: z.string().min(1).max(80),
  })
  .strict();

export const traceEntities = z
  .object({
    plans: z.array(planEntity).default([]),
    prs: z.array(prEntity).default([]),
    sessions: z.array(sessionEntity).default([]),
  })
  .strict();

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

const eventBase = {
  id,
  at: isoTimestamp,
  /** Optional caption override; scenes fall back to their own wording. */
  note: z.string().min(1).max(200).optional(),
};

const planRef = { plan: id };
const prRef = { pr: id };

export const traceEvent = z.discriminatedUnion("kind", [
  z.object({ ...eventBase, kind: z.literal("plan.drafted"), ...planRef }).strict(),
  z.object({ ...eventBase, kind: z.literal("plan.vetted"), ...planRef }).strict(),
  z
    .object({ ...eventBase, kind: z.literal("plan.implementing"), ...planRef, session: id.optional() })
    .strict(),
  z.object({ ...eventBase, kind: z.literal("plan.shipped"), ...planRef }).strict(),
  z.object({ ...eventBase, kind: z.literal("session.started"), session: id, plan: id.optional() }).strict(),
  z.object({ ...eventBase, kind: z.literal("session.ended"), session: id }).strict(),
  z.object({ ...eventBase, kind: z.literal("pr.opened"), ...prRef }).strict(),
  z.object({ ...eventBase, kind: z.literal("ci.started"), ...prRef }).strict(),
  z
    .object({
      ...eventBase,
      kind: z.literal("ci.passed"),
      ...prRef,
      checks: z.number().int().nonnegative().optional(),
    })
    .strict(),
  z
    .object({
      ...eventBase,
      kind: z.literal("ci.failed"),
      ...prRef,
      checks: z.number().int().nonnegative().optional(),
    })
    .strict(),
  z.object({ ...eventBase, kind: z.literal("merge_train.queued"), ...prRef }).strict(),
  z.object({ ...eventBase, kind: z.literal("pr.landed"), ...prRef }).strict(),
]);

export type TraceEvent = z.infer<typeof traceEvent>;
export type EventKind = TraceEvent["kind"];
export type EventOfKind<K extends EventKind> = Extract<TraceEvent, { kind: K }>;

export const EVENT_KINDS = [
  "plan.drafted",
  "plan.vetted",
  "plan.implementing",
  "plan.shipped",
  "session.started",
  "session.ended",
  "pr.opened",
  "ci.started",
  "ci.passed",
  "ci.failed",
  "merge_train.queued",
  "pr.landed",
] as const satisfies readonly EventKind[];

// ---------------------------------------------------------------------------
// Metadata (plan D2: every output carries its mode; a replay states its
// date range and source; a synthetic trace is never presented as real)
// ---------------------------------------------------------------------------

/**
 * Capped so the on-screen mode line (date range + source) fits in at most
 * three wrapped lines at the narrowest aspect (9:16); see ModeLabel.
 */
export const SOURCE_REF_MAX = 120;
const sourceRef = z.string().min(1).max(SOURCE_REF_MAX);

export const traceSource = z.discriminatedUnion("kind", [
  z
    .object({
      kind: z.literal("hand-authored"),
      /** Who/what authored it, e.g. a file path. */
      ref: sourceRef,
    })
    .strict(),
  z
    .object({
      kind: z.literal("build-record"),
      /** The export this replay was built from, e.g. a repo set or URL. Shown on screen. */
      ref: sourceRef,
      /** The build-record export version `fromBuildRecord` was pinned to. */
      export_version: z.string().min(1).max(40),
    })
    .strict(),
]);

export const dateRange = z
  .object({
    from: isoTimestamp,
    to: isoTimestamp,
  })
  .strict();

export const traceMeta = z
  .object({
    mode: z.enum(["illustrative", "replay"]),
    title: z.string().min(1).max(120),
    source: traceSource,
    date_range: dateRange.optional(),
  })
  .strict();

export type TraceMode = z.infer<typeof traceMeta>["mode"];

// ---------------------------------------------------------------------------
// The trace
// ---------------------------------------------------------------------------

const traceShape = z
  .object({
    trace_version: z.literal(TRACE_VERSION),
    meta: traceMeta,
    entities: traceEntities,
    events: z.array(traceEvent).min(1),
  })
  .strict();

/** Every string value in `value`, with its path. */
function* strings(value: unknown, path: (string | number)[] = []): Generator<[(string | number)[], string]> {
  if (typeof value === "string") yield [path, value];
  else if (Array.isArray(value)) for (const [i, v] of value.entries()) yield* strings(v, [...path, i]);
  else if (value && typeof value === "object") {
    for (const [k, v] of Object.entries(value)) yield* strings(v, [...path, k]);
  }
}

export const sceneTrace = traceShape.superRefine((trace, ctx) => {
  const { meta } = trace;

  // Every character must have a glyph in the bundled fonts (no tofu, no
  // machine-dependent fallback font).
  for (const [path, value] of strings(trace)) {
    const missing = uncoveredCharacters(value);
    if (missing.length) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path,
        message: `characters the bundled fonts cannot draw: ${missing.map(describeCharacter).join(", ")}`,
      });
    }
  }

  if (meta.mode === "replay") {
    if (!meta.date_range) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["meta", "date_range"],
        message: "a replay trace must state its date range (plan D2)",
      });
    }
    if (meta.source.kind !== "build-record") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["meta", "source", "kind"],
        message: "a replay trace must come from a build record (plan D2)",
      });
    }
  } else if (meta.source.kind !== "hand-authored") {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["meta", "source", "kind"],
      message: "an illustrative trace is hand-authored; real data is a replay (plan D2)",
    });
  }

  let rangeFrom = Number.NEGATIVE_INFINITY;
  let rangeTo = Number.POSITIVE_INFINITY;
  if (meta.date_range) {
    rangeFrom = Date.parse(meta.date_range.from);
    rangeTo = Date.parse(meta.date_range.to);
    if (rangeFrom > rangeTo) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["meta", "date_range"],
        message: "date_range.from must not be after date_range.to",
      });
    }
  }

  const unique = (kind: string, ids: string[]) => {
    const seen = new Set<string>();
    ids.forEach((value, i) => {
      if (seen.has(value)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: kind === "events" ? ["events", i, "id"] : ["entities", kind, i, "id"],
          message: `duplicate ${kind} id "${value}"`,
        });
      }
      seen.add(value);
    });
    return seen;
  };
  const plans = unique("plans", trace.entities.plans.map((p) => p.id));
  const prs = unique("prs", trace.entities.prs.map((p) => p.id));
  const sessions = unique("sessions", trace.entities.sessions.map((s) => s.id));
  unique("events", trace.events.map((e) => e.id));

  trace.entities.prs.forEach((pr, i) => {
    if (!plans.has(pr.plan)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["entities", "prs", i, "plan"],
        message: `unknown plan "${pr.plan}"`,
      });
    }
  });

  trace.events.forEach((event, i) => {
    const ref = (field: string, set: Set<string>, value: string | undefined) => {
      if (value !== undefined && !set.has(value)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["events", i, field],
          message: `unknown ${field} "${value}"`,
        });
      }
    };
    if ("plan" in event) ref("plan", plans, event.plan);
    if ("pr" in event) ref("pr", prs, event.pr);
    if ("session" in event) ref("session", sessions, event.session);

    const at = Date.parse(event.at);
    if (at < rangeFrom || at > rangeTo) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["events", i, "at"],
        message: "event lies outside meta.date_range",
      });
    }
  });
});

export type SceneTrace = z.infer<typeof sceneTrace>;
/** The JSON as authored (entity lists may be omitted). */
export type SceneTraceInput = z.input<typeof sceneTrace>;

export type TraceValidation =
  | { ok: true; trace: SceneTrace }
  | { ok: false; errors: string[] };

/** Validate untrusted JSON; never throws. */
export function validateTrace(input: unknown): TraceValidation {
  const result = sceneTrace.safeParse(input);
  if (result.success) return { ok: true, trace: result.data };
  return {
    ok: false,
    errors: result.error.issues.map((issue) => {
      const path = issue.path.length ? issue.path.join(".") : "(root)";
      return `${path}: ${issue.message}`;
    }),
  };
}

/** Validate and return the trace, or throw with every problem listed. */
export function parseTrace(input: unknown): SceneTrace {
  const result = validateTrace(input);
  if (!result.ok) {
    throw new Error(`invalid scene trace:\n  ${result.errors.join("\n  ")}`);
  }
  return result.trace;
}
