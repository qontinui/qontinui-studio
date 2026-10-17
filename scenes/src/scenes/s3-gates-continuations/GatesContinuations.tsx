import { formatDate, formatElapsed, type Layout, type SceneProps } from "../../api";
import { Caption, Counter, Edge, Feed, Gate, Node, SceneHeader, SessionLane, Timeline, type Point } from "../../primitives";
import { GATE_ROWS, s3State } from "./model";

interface Box {
  x: number;
  y: number;
  width: number;
}

interface Placement {
  header: Box & { titleSize: number };
  registrant: Point;
  continuation: Point;
  node: { width: number; height: number };
  gate: Point & { size: number };
  /** Where the edges meet the nodes and the gate. */
  edgeIn: { from: Point; to: Point };
  edgeOut: { from: Point; to: Point };
  caption: Box & { lines: number };
  /** The gates panel; null where there is no room for it. */
  gates: Box | null;
  counters: { running: Point; waited: Point };
  lanes: Box & { step: number };
  axis: Box;
}

/**
 * Where everything goes, per orientation. Pure function of the layout. The
 * header starts below the room the mode label needs at its longest.
 */
function place(layout: Layout): Placement {
  const { safe, unit: u } = layout;
  const right = safe.x + safe.width;

  if (layout.orientation === "landscape") {
    const rowY = 360 * u;
    const nodeW = 380 * u;
    const registrant = { x: safe.x + nodeW / 2, y: rowY };
    const continuation = { x: right - nodeW / 2, y: rowY };
    const gate = { x: safe.x + safe.width / 2, y: rowY, size: 150 * u };
    return {
      header: { x: safe.x, y: 140 * u, width: safe.width, titleSize: 48 },
      registrant,
      continuation,
      node: { width: nodeW, height: 104 * u },
      gate,
      edgeIn: { from: { x: registrant.x + nodeW / 2, y: rowY }, to: { x: gate.x - 80 * u, y: rowY } },
      edgeOut: { from: { x: gate.x + 80 * u, y: rowY }, to: { x: continuation.x - nodeW / 2, y: rowY } },
      caption: { x: safe.x, y: 510 * u, width: 900 * u, lines: 2 },
      gates: { x: right - 688 * u, y: 510 * u, width: 688 * u },
      counters: { running: { x: safe.x, y: 672 * u }, waited: { x: safe.x + 420 * u, y: 672 * u } },
      lanes: { x: safe.x, y: 778 * u, width: safe.width, step: 58 * u },
      axis: { x: safe.x, y: 964 * u, width: safe.width },
    };
  }

  if (layout.orientation === "portrait") {
    const cx = safe.x + safe.width / 2;
    const registrant = { x: cx, y: 420 * u };
    const gate = { x: cx, y: 600 * u, size: 150 * u };
    const continuation = { x: cx, y: 800 * u };
    const nodeH = 92 * u;
    return {
      header: { x: safe.x, y: 170 * u, width: safe.width, titleSize: 48 },
      registrant,
      continuation,
      node: { width: safe.width, height: nodeH },
      gate,
      edgeIn: { from: { x: cx, y: registrant.y + nodeH / 2 }, to: { x: cx, y: gate.y - 75 * u } },
      edgeOut: { from: { x: cx, y: gate.y + 120 * u }, to: { x: cx, y: continuation.y - nodeH / 2 } },
      caption: { x: safe.x, y: 900 * u, width: safe.width, lines: 3 },
      gates: { x: safe.x, y: 1110 * u, width: safe.width },
      counters: { running: { x: safe.x, y: 1330 * u }, waited: { x: safe.x + 480 * u, y: 1330 * u } },
      lanes: { x: safe.x, y: 1460 * u, width: safe.width, step: 66 * u },
      axis: { x: safe.x, y: 1720 * u, width: safe.width },
    };
  }

  // square: no gates panel; the gate in focus says enough.
  const rowY = 360 * u;
  const nodeW = 280 * u;
  const registrant = { x: safe.x + nodeW / 2, y: rowY };
  const continuation = { x: right - nodeW / 2, y: rowY };
  const gate = { x: safe.x + safe.width / 2, y: rowY, size: 110 * u };
  return {
    header: { x: safe.x, y: 150 * u, width: safe.width, titleSize: 36 },
    registrant,
    continuation,
    node: { width: nodeW, height: 92 * u },
    gate,
    edgeIn: { from: { x: registrant.x + nodeW / 2, y: rowY }, to: { x: gate.x - 60 * u, y: rowY } },
    edgeOut: { from: { x: gate.x + 60 * u, y: rowY }, to: { x: continuation.x - nodeW / 2, y: rowY } },
    caption: { x: safe.x, y: 480 * u, width: safe.width, lines: 2 },
    gates: null,
    counters: { running: { x: safe.x, y: 630 * u }, waited: { x: safe.x + 420 * u, y: 630 * u } },
    lanes: { x: safe.x, y: 736 * u, width: safe.width, step: 58 * u },
    axis: { x: safe.x, y: 950 * u, width: safe.width },
  };
}

/** S3 — a session registers a gate and ends; the condition is observed; a new session continues. */
export function GatesContinuations({ trace, frame, layout, timeline }: SceneProps) {
  const state = s3State(trace, timeline, frame);
  const p = place(layout);
  const u = layout.unit;
  const edge = (key: string, from: Point, to: Point, progress: number) => (
    <Edge
      key={key}
      from={from}
      to={to}
      progress={progress}
      level={progress > 0 ? "done" : "inert"}
      unit={u}
      frameWidth={layout.width}
      frameHeight={layout.height}
    />
  );

  return (
    <>
      <SceneHeader
        x={p.header.x}
        y={p.header.y}
        width={p.header.width}
        eyebrow="GATES AND CONTINUATIONS"
        title={state.title}
        titleSize={p.header.titleSize}
        unit={u}
      />

      {edge("edge-in", p.edgeIn.from, p.edgeIn.to, state.edgeIn)}
      {edge("edge-out", p.edgeOut.from, p.edgeOut.to, state.edgeOut)}

      {state.registrant ? (
        <Node
          x={p.registrant.x}
          y={p.registrant.y}
          width={p.node.width}
          height={p.node.height}
          label={state.registrant.label}
          sublabel={state.registrant.sublabel}
          level={state.registrant.level}
          appear={state.registrant.appear}
          unit={u}
        />
      ) : null}

      {state.gate ? (
        <Gate
          x={p.gate.x}
          y={p.gate.y}
          size={p.gate.size}
          label={state.gate.label}
          open={state.gate.open}
          level={state.gate.level}
          appear={state.gate.appear}
          unit={u}
        />
      ) : null}

      <Node
        x={p.continuation.x}
        y={p.continuation.y}
        width={p.node.width}
        height={p.node.height}
        label={state.continuation.label}
        sublabel={state.continuation.sublabel}
        level={state.continuation.level}
        appear={state.continuation.appear}
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

      {p.gates ? (
        <Feed
          x={p.gates.x}
          y={p.gates.y}
          width={p.gates.width}
          title="GATES"
          rows={state.gates}
          capacity={GATE_ROWS}
          empty="No gate registered yet"
          unit={u}
        />
      ) : null}

      <Counter x={p.counters.running.x} y={p.counters.running.y} label="SESSIONS RUNNING" value={String(state.running)} unit={u} />
      <Counter
        x={p.counters.waited.x}
        y={p.counters.waited.y}
        label="GATE WAITED"
        value={state.waitedMs === null ? "–" : formatElapsed(state.waitedMs)}
        unit={u}
      />

      {state.lanes.map((lane, i) => (
        <SessionLane
          key={lane.id}
          x={p.lanes.x}
          y={p.lanes.y + i * p.lanes.step}
          width={p.lanes.width}
          label={lane.label}
          start={lane.start}
          end={lane.end}
          level={lane.level}
          unit={u}
        />
      ))}

      <Timeline
        x={p.axis.x}
        y={p.axis.y}
        width={p.axis.width}
        ticks={state.ticks}
        playhead={state.playhead}
        startLabel={formatDate(state.range.fromMs)}
        endLabel={formatDate(state.range.toMs)}
        unit={u}
      />
    </>
  );
}
