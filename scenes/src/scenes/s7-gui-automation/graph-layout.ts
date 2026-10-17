/**
 * Where S7 draws its state graph, per orientation, and which graphs it can
 * draw honestly. Shared by the component and the scene's check, so the check
 * refuses exactly the graphs the layout would draw wrongly.
 */
import { ASPECTS, layoutFor, sortEvents, type Layout, type SceneTrace } from "../../api";

/** States the layouts have slots for. */
export const MAX_STATES = 6;

export interface GraphSlots {
  /** Centre of each state slot, in discovery order. */
  slots: { x: number; y: number }[];
  node: { width: number; height: number };
}

/** Slot centres for a cols x rows grid, filled as a snake. */
function snake(cols: number, rows: number, x: (c: number) => number, y: (r: number) => number) {
  const out: { x: number; y: number }[] = [];
  for (let r = 0; r < rows; r++) {
    for (let k = 0; k < cols; k++) {
      const c = r % 2 === 0 ? k : cols - 1 - k;
      out.push({ x: x(c), y: y(r) });
    }
  }
  return out.slice(0, MAX_STATES);
}

export function graphSlots(layout: Layout): GraphSlots {
  const { safe, unit: u } = layout;
  if (layout.orientation === "landscape") {
    return {
      slots: snake(3, 2, (c) => safe.x + 120 * u + c * 450 * u, (r) => 420 * u + r * 300 * u),
      node: { width: 220 * u, height: 84 * u },
    };
  }
  if (layout.orientation === "portrait") {
    // Two columns, four rows: the path turns at every row, so a chord from
    // the second state to the fifth runs straight down.
    const col = (c: number) => safe.x + 150 * u + c * 636 * u;
    const row = (r: number) => 420 * u + r * 210 * u;
    return {
      slots: [
        { x: col(0), y: row(0) },
        { x: col(0), y: row(1) },
        { x: col(1), y: row(1) },
        { x: col(1), y: row(2) },
        { x: col(0), y: row(2) },
        { x: col(0), y: row(3) },
      ],
      node: { width: 300 * u, height: 84 * u },
    };
  }
  return {
    slots: snake(3, 2, (c) => safe.x + 110 * u + c * 358 * u, (r) => 380 * u + r * 220 * u),
    node: { width: 200 * u, height: 72 * u },
  };
}

/** Each found state's slot: discovery order, over the whole trace. */
export function discoverySlots(trace: SceneTrace): Map<string, number> {
  const slots = new Map<string, number>();
  for (const e of sortEvents(trace.events)) {
    if (e.kind === "gui.state_found" && !slots.has(e.state)) slots.set(e.state, slots.size);
  }
  return slots;
}

interface P {
  x: number;
  y: number;
}

/** Does the segment a→b enter the w x h box centred on c? (Liang–Barsky, exact.) */
function crosses(a: P, b: P, c: P, w: number, h: number): boolean {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  let t0 = 0;
  let t1 = 1;
  const edges: [number, number][] = [
    [-dx, a.x - (c.x - w / 2)],
    [dx, c.x + w / 2 - a.x],
    [-dy, a.y - (c.y - h / 2)],
    [dy, c.y + h / 2 - a.y],
  ];
  for (const [pp, q] of edges) {
    if (pp === 0) {
      if (q <= 0) return false;
      continue;
    }
    const r = q / pp;
    if (pp < 0) t0 = Math.max(t0, r);
    else t1 = Math.min(t1, r);
    if (t0 >= t1) return false;
  }
  return true;
}

export interface LabelPlacement {
  x: number;
  y: number;
  maxWidth: number;
}

/**
 * Where an edge's action label goes: above the row for an in-row edge where
 * the rows are close (landscape, square), otherwise on the middle of its line.
 */
export function labelPlacement(layout: Layout, g: GraphSlots, a: P, b: P): LabelPlacement {
  const u = layout.unit;
  const across = Math.abs(b.y - a.y) < g.node.height;
  const above = across && layout.orientation !== "portrait";
  const x = (a.x + b.x) / 2;
  const y = above ? Math.min(a.y, b.y) - g.node.height / 2 - 22 * u : (a.y + b.y) / 2;
  // Room: an in-row label above spans the gap between centres; on the line it
  // must stay between the two nodes; an up-and-down one may be a little wider
  // than a node; a diagonal one stays within the gap between columns.
  const columnGap = Math.min(
    ...g.slots.flatMap((p, i) => g.slots.slice(i + 1).map((q) => Math.abs(q.x - p.x)).filter((d) => d > 1)),
  );
  const maxWidth = above
    ? Math.abs(b.x - a.x) - 20 * u
    : across
      ? Math.abs(b.x - a.x) - g.node.width - 20 * u
      : Math.abs(b.x - a.x) < 1
        ? g.node.width + 40 * u
        : Math.max(80 * u, columnGap - g.node.width - 20 * u);
  return { x, y, maxWidth };
}

/** The box a label will occupy (its text estimated, capped at its room), for overlap checks. */
function labelBox(layout: Layout, place: LabelPlacement, text: string) {
  const u = layout.unit;
  const estimated = text.length * 0.56 * 22 * 0.85 * u + 22 * u;
  const width = Math.min(Math.max(80 * u, place.maxWidth), Math.max(80 * u, estimated));
  return { x: place.x, y: place.y, w: width, h: 30 * u };
}

function overlap(p: { x: number; y: number; w: number; h: number }, q: { x: number; y: number; w: number; h: number }) {
  return Math.abs(p.x - q.x) < (p.w + q.w) / 2 && Math.abs(p.y - q.y) < (p.h + q.h) / 2;
}

/**
 * Why the layouts cannot draw this trace's graph honestly; empty when they
 * can. Refused: more found states than slots, a self-loop, two transitions
 * between the same two states (either way), and an edge that would run
 * through another state's box in any aspect.
 */
export function graphProblems(trace: SceneTrace): string[] {
  const problems: string[] = [];
  const slotOf = discoverySlots(trace);
  if (slotOf.size > MAX_STATES) problems.push(`at most ${MAX_STATES} states found (the trace finds ${slotOf.size})`);
  const found = new Set(sortEvents(trace.events).flatMap((e) => (e.kind === "gui.transition_found" ? [e.transition] : [])));
  const edges = trace.entities.transitions.filter((t) => found.has(t.id));
  const pairs = new Set<string>();
  for (const t of edges) {
    if (t.from === t.to) {
      problems.push(`no self-loop (${t.id})`);
      continue;
    }
    const pair = [t.from, t.to].sort().join("|");
    if (pairs.has(pair)) problems.push(`one transition per pair of states (${t.id} repeats ${t.from}/${t.to})`);
    pairs.add(pair);
    for (const aspect of ASPECTS) {
      const g = graphSlots(layoutFor(aspect));
      const a = g.slots[slotOf.get(t.from) ?? -1];
      const b = g.slots[slotOf.get(t.to) ?? -1];
      if (!a || !b) continue;
      const blocked = [...slotOf.entries()].find(([id, slot]) => {
        const c = g.slots[slot];
        return id !== t.from && id !== t.to && c !== undefined && crosses(a, b, c, g.node.width, g.node.height);
      });
      if (blocked) {
        problems.push(`${t.id} would run through "${blocked[0]}" at ${aspect}`);
        break;
      }
    }
  }
  // Every action label must sit clear of every other label and every state.
  for (const aspect of ASPECTS) {
    const layout = layoutFor(aspect);
    const g = graphSlots(layout);
    const boxes = edges.flatMap((t) => {
      const a = g.slots[slotOf.get(t.from) ?? -1];
      const b = g.slots[slotOf.get(t.to) ?? -1];
      return a && b && t.from !== t.to ? [{ id: t.id, box: labelBox(layout, labelPlacement(layout, g, a, b), t.action) }] : [];
    });
    const nodes = [...slotOf.entries()].flatMap(([id, slot]) => {
      const c = g.slots[slot];
      return c ? [{ id, box: { x: c.x, y: c.y, w: g.node.width, h: g.node.height } }] : [];
    });
    const clash = boxes.flatMap((p, i) => [
      ...boxes.slice(i + 1).filter((q) => overlap(p.box, q.box)).map((q) => `the labels of ${p.id} and ${q.id} overlap at ${aspect}`),
      ...nodes.filter((n) => overlap(p.box, n.box)).map((n) => `the label of ${p.id} covers "${n.id}" at ${aspect}`),
    ]);
    if (clash.length) {
      problems.push(...clash);
      break;
    }
  }
  return problems;
}
