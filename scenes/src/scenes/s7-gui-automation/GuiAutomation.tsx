import type { Layout, SceneProps } from "../../api";
import { Caption, Counter, Edge, Feed, Label, Node, SceneHeader, type Point } from "../../primitives";
import { graphSlots } from "./graph-layout";
import { MODEL_ROWS, s7State } from "./model";

interface Box {
  x: number;
  y: number;
  width: number;
}

interface Placement {
  header: Box & { titleSize: number };
  /** Centre of each state slot, in discovery order (see graph-layout.ts). */
  slots: Point[];
  node: { width: number; height: number };
  /** Put an in-row edge's action on its line instead of above the row. */
  inRowLabelsOnLine: boolean;
  /** The learned-model panel; null where there is no room (square). */
  model: Box | null;
  counters: Point[];
  caption: Box & { lines: number };
}

/** Where everything goes, per orientation. Pure function of the layout. */
function place(layout: Layout): Placement {
  const { safe, unit: u } = layout;
  const right = safe.x + safe.width;

  if (layout.orientation === "landscape") {
    const sideX = safe.x + 1290 * u;
    return {
      header: { x: safe.x, y: 140 * u, width: safe.width, titleSize: 48 },
      ...graphSlots(layout),
      inRowLabelsOnLine: false,
      model: { x: sideX, y: 330 * u, width: right - sideX },
      counters: [
        { x: sideX, y: 650 * u },
        { x: sideX + 200 * u, y: 650 * u },
      ],
      caption: { x: safe.x, y: 820 * u, width: 1180 * u, lines: 2 },
    };
  }

  if (layout.orientation === "portrait") {
    return {
      header: { x: safe.x, y: 170 * u, width: safe.width, titleSize: 48 },
      ...graphSlots(layout),
      // Rows are far apart here, so an in-row action sits on its own line.
      inRowLabelsOnLine: true,
      model: { x: safe.x, y: 1140 * u, width: safe.width },
      counters: [
        { x: safe.x, y: 1460 * u },
        { x: safe.x + 480 * u, y: 1460 * u },
      ],
      caption: { x: safe.x, y: 1590 * u, width: safe.width, lines: 2 },
    };
  }

  // square: the graph across the top, the caption and counters below.
  return {
    header: { x: safe.x, y: 150 * u, width: safe.width, titleSize: 36 },
    ...graphSlots(layout),
    inRowLabelsOnLine: false,
    model: null,
    counters: [
      { x: safe.x, y: 850 * u },
      { x: safe.x + 300 * u, y: 850 * u },
    ],
    caption: { x: safe.x, y: 690 * u, width: safe.width, lines: 2 },
  };
}

/** S7 — a state graph learned from an app, then traversed by actions. */
export function GuiAutomation({ trace, frame, layout, timeline }: SceneProps) {
  const state = s7State(trace, timeline, frame);
  const p = place(layout);
  const u = layout.unit;
  const at = (slot: number) => p.slots[slot] ?? (p.slots[0] as Point);

  return (
    <>
      <SceneHeader
        x={p.header.x}
        y={p.header.y}
        width={p.header.width}
        eyebrow="MODEL-BASED GUI AUTOMATION"
        title={state.title}
        titleSize={p.header.titleSize}
        unit={u}
      />

      {state.edges.map((edge) => (
        <Edge
          key={`edge-${edge.id}`}
          from={at(edge.fromSlot)}
          to={at(edge.toSlot)}
          progress={edge.progress}
          level={edge.level}
          unit={u}
          frameWidth={layout.width}
          frameHeight={layout.height}
        />
      ))}

      {state.states.map((s) => {
        const c = at(s.slot);
        return (
          <Node
            key={s.id}
            x={c.x}
            y={c.y}
            width={p.node.width}
            height={p.node.height}
            label={s.label}
            level={s.level}
            appear={s.appear}
            emphasis={s.emphasis}
            unit={u}
          />
        );
      })}

      {state.edges.map((edge) => {
        const a = at(edge.fromSlot);
        const b = at(edge.toSlot);
        const across = Math.abs(b.y - a.y) < p.node.height;
        // An edge within a row has its action above the line, clear of the
        // nodes; any other edge has it on the middle of its line.
        const x = (a.x + b.x) / 2;
        const above = across && !p.inRowLabelsOnLine;
        const y = above ? Math.min(a.y, b.y) - p.node.height / 2 - 22 * u : (a.y + b.y) / 2;
        const maxWidth = above
          ? Math.abs(b.x - a.x) - 20 * u
          : across
            ? Math.abs(b.x - a.x) - p.node.width - 20 * u
            : p.node.width + 40 * u;
        return <Label key={`label-${edge.id}`} x={x} y={y} text={edge.action} maxWidth={maxWidth} appear={edge.appear} unit={u} />;
      })}

      {p.model ? (
        <Feed
          x={p.model.x}
          y={p.model.y}
          width={p.model.width}
          title={`THE LEARNED MODEL · LATEST ${MODEL_ROWS}`}
          rows={state.model}
          capacity={MODEL_ROWS}
          empty="Exploring the app"
          unit={u}
        />
      ) : null}

      <Counter x={(p.counters[0] as Point).x} y={(p.counters[0] as Point).y} label="STATES" value={String(state.statesLearned)} unit={u} />
      <Counter
        x={(p.counters[1] as Point).x}
        y={(p.counters[1] as Point).y}
        label={state.phase === "traversing" ? "ACTIONS TAKEN" : "ACTIONS LEARNED"}
        value={String(state.phase === "traversing" ? state.steps : state.actionsLearned)}
        unit={u}
      />

      {state.caption ? (
        <Caption
          x={p.caption.x}
          y={p.caption.y}
          width={p.caption.width}
          title={state.caption.title}
          body={state.caption.body}
          appear={state.caption.appear}
          maxBodyLines={p.caption.lines}
          unit={u}
        />
      ) : null}
    </>
  );
}
