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

/** Does the segment a→b pass through the box centred on c (sampled, so deterministic)? */
function crosses(a: { x: number; y: number }, b: { x: number; y: number }, c: { x: number; y: number }, w: number, h: number) {
  for (let i = 1; i < 64; i++) {
    const t = i / 64;
    const x = a.x + (b.x - a.x) * t;
    const y = a.y + (b.y - a.y) * t;
    if (Math.abs(x - c.x) < w / 2 && Math.abs(y - c.y) < h / 2) return true;
  }
  return false;
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
  return problems;
}
