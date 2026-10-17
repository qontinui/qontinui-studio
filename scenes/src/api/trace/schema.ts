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
    /** The machine it runs on (S5), from `entities.machines`. */
    machine: id.optional(),
  })
  .strict();

/**
 * A machine in the fleet (S5). Display label only, e.g. "Build box A":
 * never a real host name (sibling D3).
 */
export const machineEntity = z
  .object({
    id,
    label: z.string().min(1).max(24),
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

/** A state of an application's GUI, as a learned model sees it (S7). */
export const guiStateEntity = z
  .object({
    id,
    label: z.string().min(1).max(24),
  })
  .strict();

/** A transition between two GUI states, made by an action (S7). */
export const guiTransitionEntity = z
  .object({
    id,
    from: id,
    to: id,
    /** The action that makes it, e.g. "click Sign in". */
    action: z.string().min(1).max(32),
  })
  .strict();

export const traceEntities = z
  .object({
    plans: z.array(planEntity).default([]),
    prs: z.array(prEntity).default([]),
    sessions: z.array(sessionEntity).default([]),
    sources: z.array(sourceEntity).default([]),
    gates: z.array(gateEntity).default([]),
    machines: z.array(machineEntity).default([]),
    states: z.array(guiStateEntity).default([]),
    transitions: z.array(guiTransitionEntity).default([]),
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
  // Self-correction (S6): a red run is diagnosed and a fix pushed, with no
  // human step; the next ci.started/ci.passed closes the loop.
  z.object({ ...eventBase, kind: z.literal("ci.diagnosed"), ...prRef, cause: z.string().min(1).max(80) }).strict(),
  z.object({ ...eventBase, kind: z.literal("fix.pushed"), ...prRef, summary: z.string().min(1).max(80) }).strict(),
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
  // Model-based GUI automation (S7): states and transitions are found while
  // exploring an application, then the learned graph is traversed by actions.
  z.object({ ...eventBase, kind: z.literal("gui.state_found"), state: id }).strict(),
  z.object({ ...eventBase, kind: z.literal("gui.transition_found"), transition: id }).strict(),
  z.object({ ...eventBase, kind: z.literal("gui.transition_taken"), transition: id }).strict(),
  // The fleet (S5): sessions in isolated worktrees coordinate through declared
  // intent and claims. A contended claim names the session holding it.
  z
    .object({ ...eventBase, kind: z.literal("worktree.allocated"), session: id, repo: z.string().min(1).max(60) })
    .strict(),
  z
    .object({ ...eventBase, kind: z.literal("intent.declared"), session: id, intent: z.string().min(1).max(80) })
    .strict(),
  z
    .object({
      ...eventBase,
      kind: z.literal("claim.acquired"),
      session: id,
      /**
       * What is claimed, as display text. Illustrative traces may name a file;
       * a replay adapter must not feed real paths here (they can leak private
       * structure) — map to a coarse kind instead.
       */
      resource: z.string().min(1).max(60),
    })
    .strict(),
  z
    .object({
      ...eventBase,
      kind: z.literal("claim.contended"),
      session: id,
      resource: z.string().min(1).max(60),
      /** The session that holds the claim. */
      holder: id,
    })
    .strict(),
  z
    .object({ ...eventBase, kind: z.literal("claim.released"), session: id, resource: z.string().min(1).max(60) })
    .strict(),
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
  "ci.diagnosed",
  "fix.pushed",
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
  "gui.state_found",
  "gui.transition_found",
  "gui.transition_taken",
  "worktree.allocated",
  "intent.declared",
  "claim.acquired",
  "claim.contended",
  "claim.released",
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
  const machines = unique("machines", trace.entities.machines.map((m) => m.id));
  const guiStates = unique("states", trace.entities.states.map((st) => st.id));
  const guiTransitions = unique("transitions", trace.entities.transitions.map((t) => t.id));
  trace.entities.transitions.forEach((t, k) => {
    for (const end of ["from", "to"] as const) {
      if (!guiStates.has(t[end])) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["entities", "transitions", k, end],
          message: `unknown state "${t[end]}"`,
        });
      }
    }
  });
  trace.entities.sessions.forEach((session, i) => {
    if (session.machine !== undefined && !machines.has(session.machine)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["entities", "sessions", i, "machine"],
        message: `unknown machine "${session.machine}"`,
      });
    }
  });
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

  // The fleet (S5), in one pass in timeline order: who holds each claim, and
  // where each session is in its life, just before every event. A session's
  // end releases its claims (its worktree is reclaimed with them).
  const FLEET_KINDS: readonly EventKind[] = [
    "worktree.allocated",
    "intent.declared",
    "claim.acquired",
    "claim.contended",
    "claim.released",
  ];
  const holderBefore = new Map<number, string | undefined>();
  const lifeBefore = new Map<number, "unstarted" | "running" | "ended">();
  const lifeRepeats = new Map<number, string>();
  {
    const order = trace.events.map((_, j) => j).sort((a, b) => (precedes(a, b) ? -1 : precedes(b, a) ? 1 : 0));
    const holder = new Map<string, string>();
    const life = new Map<string, "running" | "ended">();
    for (const j of order) {
      const e = trace.events[j] as TraceEvent;
      if ("resource" in e) holderBefore.set(j, holder.get(e.resource));
      if (FLEET_KINDS.includes(e.kind) && "session" in e && e.session !== undefined) {
        lifeBefore.set(j, life.get(e.session) ?? "unstarted");
      }
      if (e.kind === "session.started") {
        // A session lives once: a second start (or a start after its end) is refused below.
        if (life.has(e.session)) lifeRepeats.set(j, `"${e.session}" starts more than once`);
        else life.set(e.session, "running");
      } else if (e.kind === "session.ended") {
        if (life.get(e.session) === "ended") lifeRepeats.set(j, `"${e.session}" ends more than once`);
        life.set(e.session, "ended");
        for (const [resource, who] of [...holder]) if (who === e.session) holder.delete(resource);
      } else if (e.kind === "claim.acquired" && !holder.has(e.resource) && life.get(e.session) !== "ended") {
        // An acquire refused for acting after the end must not take the claim either.
        holder.set(e.resource, e.session);
      } else if (e.kind === "claim.released" && holder.get(e.resource) === e.session) holder.delete(e.resource);
    }
  }
  const startedSessions = new Set(trace.events.flatMap((e) => (e.kind === "session.started" ? [e.session] : [])));

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
    if (event.kind === "gui.state_found") ref("state", guiStates, event.state);
    if (event.kind === "gui.transition_found" || event.kind === "gui.transition_taken") {
      ref("transition", guiTransitions, event.transition);
    }
    if (event.kind === "gui.state_found" || event.kind === "gui.transition_found") {
      const key = event.kind === "gui.state_found" ? event.state : event.transition;
      const keyOf = (e: TraceEvent) =>
        e.kind === "gui.state_found" ? e.state : e.kind === "gui.transition_found" ? e.transition : undefined;
      const again = trace.events.some((e, j) => e.kind === event.kind && keyOf(e) === key && j !== i && precedes(j, i));
      if (again) issue(event.kind === "gui.state_found" ? "state" : "transition", `"${key}" is found more than once`);
    }
    if (event.kind === "gui.transition_found" || event.kind === "gui.transition_taken") {
      const t = trace.entities.transitions.find((x) => x.id === event.transition);
      const foundBefore = (kind: "gui.state_found" | "gui.transition_found", key: string) =>
        trace.events.some(
          (e, j) =>
            precedes(j, i) &&
            ((kind === "gui.state_found" && e.kind === "gui.state_found" && e.state === key) ||
              (kind === "gui.transition_found" && e.kind === "gui.transition_found" && e.transition === key)),
        );
      if (t && event.kind === "gui.transition_found") {
        // A transition is learned between two states already found.
        for (const end of [t.from, t.to]) {
          if (guiStates.has(end) && !foundBefore("gui.state_found", end)) {
            issue("transition", `"${t.id}" is found before its state "${end}"`);
          }
        }
      }
      if (t && event.kind === "gui.transition_taken") {
        if (!foundBefore("gui.transition_found", t.id)) issue("transition", `"${t.id}" is taken before it is found`);
        // A traversal is a path: each step starts where the previous one ended.
        let previous = -1;
        trace.events.forEach((e, j) => {
          if (e.kind === "gui.transition_taken" && precedes(j, i) && (previous < 0 || precedes(previous, j))) previous = j;
        });
        const prev = previous >= 0 ? trace.events[previous] : undefined;
        const prevT =
          prev && prev.kind === "gui.transition_taken" ? trace.entities.transitions.find((x) => x.id === prev.transition) : undefined;
        if (prevT && prevT.to !== t.from) {
          issue("transition", `"${t.id}" starts at "${t.from}", but the traversal is at "${prevT.to}"`);
        }
      }
    }
    const repeat = lifeRepeats.get(i);
    if (repeat) issue("session", repeat);
    if (FLEET_KINDS.includes(event.kind) && "session" in event && event.session !== undefined) {
      const life = lifeBefore.get(i);
      if (life === "ended") issue("session", `"${event.session}" acts after it ended (${event.kind})`);
      else if (life === "unstarted" && startedSessions.has(event.session)) {
        issue("session", `"${event.session}" acts before it started (${event.kind})`);
      }
    }
    if (event.kind === "claim.contended") {
      ref("holder", sessions, event.holder, "session");
      if (event.holder === event.session) issue("holder", "a session cannot contend with itself");
      else if (sessions.has(event.holder) && holderBefore.get(i) !== event.holder) {
        issue("holder", `"${event.holder}" does not hold "${event.resource}" at that point`);
      }
    }
    if (event.kind === "claim.released" && holderBefore.get(i) !== event.session) {
      issue("resource", `"${event.session}" releases "${event.resource}" without holding it`);
    }
    if (event.kind === "claim.acquired") {
      const held = holderBefore.get(i);
      if (held === event.session) issue("resource", `"${event.session}" already holds "${event.resource}"`);
      else if (held !== undefined) issue("resource", `"${event.resource}" is already held by "${held}"`);
    }
    if (prs.has("pr" in event ? event.pr : "") && "pr" in event) {
      const pr = event.pr;
      // Two independent flags, replayed in timeline order up to this event:
      // an unresolved conflict (set by conflicted; cleared by resolved or a
      // re-queue) and red CI (set by ci.failed; cleared by ci.passed or a
      // re-queue).
      let conflict = false;
      let red = false;
      // Self-correction (S6): a diagnosis since the latest red run, and a fix
      // since that diagnosis.
      let diagnosed = false;
      let fixed = false;
      trace.events
        .map((e, j) => ({ e, j }))
        .filter(({ e, j }) => "pr" in e && e.pr === pr && precedes(j, i))
        .sort((a, b) => (precedes(a.j, b.j) ? -1 : 1))
        .forEach(({ e }) => {
          if (e.kind === "merge_train.conflicted") conflict = true;
          else if (e.kind === "merge_train.resolved") conflict = false;
          else if (e.kind === "ci.failed") {
            red = true;
            diagnosed = fixed = false;
          } else if (e.kind === "ci.passed") red = diagnosed = fixed = false;
          else if (e.kind === "merge_train.queued") conflict = red = false;
          else if (e.kind === "ci.diagnosed") {
            diagnosed = true;
            fixed = false;
          } else if (e.kind === "fix.pushed") fixed = true;
        });
      if (event.kind === "ci.diagnosed" && !red) issue("pr", `ci.diagnosed while "${pr}" is not red`);
      if (event.kind === "fix.pushed" && (!red || !diagnosed || fixed)) {
        issue("pr", `fix.pushed without a diagnosis of "${pr}"'s latest red run that is not yet fixed`);
      }
      if (event.kind === "merge_train.resolved" && !conflict) {
        issue("pr", `merge_train.resolved without an unresolved merge_train.conflicted for "${pr}"`);
      }
      if (event.kind === "merge_train.rebased" || event.kind === "pr.landed") {
        if (conflict) issue("pr", `${event.kind} while "${pr}" has an unresolved conflict`);
        if (red) issue("pr", `${event.kind} while "${pr}" is red (it needs ci.passed or merge_train.queued first)`);
      }
      if (event.kind === "pr.landed") {
        const first = trace.events.findIndex((e, j) => e.kind === "pr.landed" && e.pr === pr && j !== i && precedes(j, i));
        if (first >= 0) issue("pr", `"${pr}" lands more than once`);
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
