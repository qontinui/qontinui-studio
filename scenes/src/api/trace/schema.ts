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

/**
 * A system whose state the factory's twin mirrors (S2). Display label only:
 * never a machine name, an account or a private repo (sibling D3).
 */
export const SOURCE_KINDS = ["repo-host", "ci", "machine", "session"] as const;
export const SOURCE_LABEL_MAX = 20;
export const sourceEntity = z
  .object({
    id,
    kind: z.enum(SOURCE_KINDS),
    /** Short: it is drawn inside a node at every aspect. */
    label: z.string().min(1).max(SOURCE_LABEL_MAX),
  })
  .strict();

/** A condition a session waits on without running (S3). */
export const gateEntity = z
  .object({
    id,
    /** What must become true, e.g. "qontinui-web#1810 has landed". */
    condition: z.string().min(1).max(60),
  })
  .strict();

export const traceEntities = z
  .object({
    plans: z.array(planEntity).default([]),
    prs: z.array(prEntity).default([]),
    sessions: z.array(sessionEntity).default([]),
    sources: z.array(sourceEntity).default([]),
    gates: z.array(gateEntity).default([]),
  })
  .strict();

export type EntityKind = keyof z.infer<typeof traceEntities>;

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
  z
    .object({
      ...eventBase,
      kind: z.literal("session.started"),
      session: id,
      plan: id.optional(),
      /** The gate whose clearing started this session (a continuation, S3). */
      continues: id.optional(),
    })
    .strict(),
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
  // The merge train (S4): the head of the train is rebased onto main; a
  // rebase that conflicts takes the PR out of the train until it is resolved.
  z.object({ ...eventBase, kind: z.literal("merge_train.rebased"), ...prRef }).strict(),
  z.object({ ...eventBase, kind: z.literal("merge_train.conflicted"), ...prRef }).strict(),
  z.object({ ...eventBase, kind: z.literal("merge_train.resolved"), ...prRef }).strict(),
  z.object({ ...eventBase, kind: z.literal("pr.landed"), ...prRef }).strict(),
  // The digital twin (S2): state mirrored from a source, and what is decided
  // from it.
  z
    .object({ ...eventBase, kind: z.literal("twin.mirrored"), source: id, fact: z.string().min(1).max(80) })
    .strict(),
  z
    .object({
      ...eventBase,
      kind: z.literal("twin.decided"),
      decision: z.string().min(1).max(80),
      /** What in the twin the decision was made from. */
      basis: z.string().min(1).max(80),
    })
    .strict(),
  // The developer twin (S2): a session's question, answered from served
  // documents rather than by waking the operator.
  z
    .object({ ...eventBase, kind: z.literal("question.asked"), session: id, question: z.string().min(1).max(120) })
    .strict(),
  z
    .object({
      ...eventBase,
      kind: z.literal("question.answered"),
      /** The id of the `question.asked` event this answers (one answer per question). */
      question: id,
      answer: z.string().min(1).max(120),
      /** Where the answer came from: a served policy or intent document, or the operator. */
      from: z.enum(["policy", "intent", "operator"]),
      /**
       * The served document cited, e.g. "policy git-operations". Required when
       * the answer came from a document; absent when the operator answered.
       */
      document: z.string().min(1).max(60).optional(),
    })
    .strict(),
  // Gates and continuations (S3): a session registers a gate on a condition
  // and ends; coord observes the condition and clears the gate; a new
  // session continues the work (session.started with `continues`).
  z.object({ ...eventBase, kind: z.literal("gate.registered"), gate: id, session: id }).strict(),
  z.object({ ...eventBase, kind: z.literal("gate.observed"), gate: id }).strict(),
  z.object({ ...eventBase, kind: z.literal("gate.cleared"), gate: id }).strict(),
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
  "merge_train.rebased",
  "merge_train.conflicted",
  "merge_train.resolved",
  "pr.landed",
  "twin.mirrored",
  "twin.decided",
  "question.asked",
  "question.answered",
  "gate.registered",
  "gate.observed",
  "gate.cleared",
] as const satisfies readonly EventKind[];

// ---------------------------------------------------------------------------
// Metadata (plan D2: every output carries its mode; a replay states its
// date range and source; a synthetic trace is never presented as real)
// ---------------------------------------------------------------------------

/**
 * Capped so a realistic on-screen mode line (date range + source) fits in the
 * three lines ModeLabel allows at 9:16. ModeLabel clamps at three lines with
 * an ellipsis regardless (e.g. 120 "W"s); the full source stays in the trace.
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
  const sources = unique("sources", trace.entities.sources.map((s) => s.id));
  const gates = unique("gates", trace.entities.gates.map((g) => g.id));
  unique("events", trace.events.map((e) => e.id));
  const eventsById = new Map(trace.events.map((e) => [e.id, e]));
  const answeredQuestions = new Set<string>();
  /** Timeline order: by time, then by position in the trace. */
  const precedes = (a: number, b: number) => {
    const ta = Date.parse((trace.events[a] as TraceEvent).at);
    const tb = Date.parse((trace.events[b] as TraceEvent).at);
    return ta < tb || (ta === tb && a < b);
  };
  /** Index of the first event of `kind` for gate `gate` in timeline order, or -1. */
  const gateEvent = (kind: EventKind, gate: string) => {
    let first = -1;
    trace.events.forEach((e, j) => {
      if (e.kind === kind && "gate" in e && e.gate === gate && (first < 0 || precedes(j, first))) first = j;
    });
    return first;
  };

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
    /** `noun` names what the field refers to when it differs from the field (e.g. continues → gate). */
    const ref = (field: string, set: Set<string>, value: string | undefined, noun = field) => {
      if (value !== undefined && !set.has(value)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["events", i, field],
          message: `unknown ${noun} "${value}"`,
        });
      }
    };
    if ("plan" in event) ref("plan", plans, event.plan);
    if ("pr" in event) ref("pr", prs, event.pr);
    if ("session" in event) ref("session", sessions, event.session);
    if ("source" in event) ref("source", sources, event.source);
    if ("gate" in event) ref("gate", gates, event.gate);
    if (event.kind === "session.started") ref("continues", gates, event.continues, "gate");
    const issue = (field: string, message: string) =>
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["events", i, field], message });
    /** `kind` for `gate` must exist and come strictly before event i. */
    const mustFollow = (kind: EventKind, gate: string, field: string, before: string, never: string) => {
      if (!gates.has(gate)) return;
      const at = gateEvent(kind, gate);
      if (at < 0) issue(field, `${never} "${gate}"`);
      else if (!precedes(at, i)) issue(field, `${before} "${gate}"`);
    };
    if (event.kind === "gate.observed" || event.kind === "gate.cleared") {
      mustFollow(
        "gate.registered",
        event.gate,
        "gate",
        `${event.kind} before gate.registered for`,
        `${event.kind} for a gate that is never registered:`,
      );
      if (gateEvent(event.kind, event.gate) !== i) issue("gate", `${event.kind} occurs more than once for "${event.gate}"`);
    }
    if (event.kind === "gate.cleared") {
      // Observed (when it is) before it clears: the condition is seen, then the gate opens.
      const seen = gateEvent("gate.observed", event.gate);
      if (seen >= 0 && !precedes(seen, i)) issue("gate", `gate.cleared before gate.observed for "${event.gate}"`);
    }
    if (event.kind === "gate.registered") {
      if (gateEvent("gate.registered", event.gate) !== i) issue("gate", `gate "${event.gate}" is registered more than once`);
      // A session registers a gate while it is running, not after it ended.
      const ended = trace.events.findIndex((e, j) => e.kind === "session.ended" && e.session === event.session && precedes(j, i));
      if (ended >= 0) issue("session", `"${event.session}" registers a gate after it ended`);
    }
    if (event.kind === "merge_train.resolved" && prs.has(event.pr)) {
      const conflicted = trace.events.findIndex(
        (e, j) => e.kind === "merge_train.conflicted" && e.pr === event.pr && precedes(j, i),
      );
      if (conflicted < 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["events", i, "pr"],
          message: `merge_train.resolved before any merge_train.conflicted for "${event.pr}"`,
        });
      }
    }
    if (event.kind === "session.started" && event.continues !== undefined) {
      mustFollow(
        "gate.cleared",
        event.continues,
        "continues",
        "a continuation starts before gate.cleared for",
        "a continuation of a gate that is never cleared:",
      );
    }
    if (event.kind === "question.answered") {
      const asked = eventsById.get(event.question);
      const askedAt = trace.events.findIndex((e) => e.id === event.question);
      if (!asked || asked.kind !== "question.asked") {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["events", i, "question"],
          message: `"${event.question}" is not a question.asked event`,
        });
      } else {
        // Timelines order by time, then by position: the answer must come
        // strictly after its question in that order.
        const askedMs = Date.parse(asked.at);
        const answeredMs = Date.parse(event.at);
        if (answeredMs < askedMs || (answeredMs === askedMs && i < askedAt)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ["events", i, "at"],
            message: `answered before question "${event.question}" was asked`,
          });
        }
        if (answeredQuestions.has(event.question)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ["events", i, "question"],
            message: `question "${event.question}" is answered more than once`,
          });
        }
        answeredQuestions.add(event.question);
      }
      if ((event.from === "operator") === (event.document !== undefined)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["events", i, "document"],
          message:
            event.from === "operator"
              ? "an operator's answer cites no document"
              : `an answer from ${event.from} must cite its document`,
        });
      }
    }

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
