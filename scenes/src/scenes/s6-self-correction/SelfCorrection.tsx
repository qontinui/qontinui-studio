import { formatElapsed, type Layout, type SceneProps } from "../../api";
import { Caption, Counter, Edge, Feed, Node, PrCard, SceneHeader, type Point } from "../../primitives";
import { LOG_ROWS, s6State } from "./model";

interface Box {
  x: number;
  y: number;
  width: number;
}

interface Placement {
  header: Box & { titleSize: number };
  /** Centres of run, red, diagnosed, fixed (a ring) and green (its exit). */
  at: Record<"run" | "red" | "diagnosed" | "fixed" | "green", Point>;
  node: { width: number; height: number };
  pr: Box;
  log: Box & { rows: number };
  /** Counters; null where there is no room (square). */
  counters: { runs: Point; toGreen: Point } | null;
  caption: Box & { lines: number };
}

/** A ring of four steps around `c` with radii `rx`/`ry`, plus the exit to the right of the top. */
function ring(c: Point, rx: number, ry: number, exitDx: number) {
  return {
    run: { x: c.x, y: c.y - ry },
    red: { x: c.x + rx, y: c.y },
    diagnosed: { x: c.x, y: c.y + ry },
    fixed: { x: c.x - rx, y: c.y },
    green: { x: c.x + exitDx, y: c.y - ry },
  };
}

/** Where everything goes, per orientation. Pure function of the layout. */
function place(layout: Layout): Placement {
  const { safe, unit: u } = layout;
  const right = safe.x + safe.width;

  if (layout.orientation === "landscape") {
    const sideX = safe.x + 1028 * u;
    return {
      header: { x: safe.x, y: 140 * u, width: safe.width, titleSize: 48 },
      at: ring({ x: safe.x + 410 * u, y: 590 * u }, 270 * u, 190 * u, 470 * u),
      node: { width: 240 * u, height: 84 * u },
      pr: { x: sideX, y: 330 * u, width: right - sideX },
      caption: { x: sideX, y: 480 * u, width: right - sideX, lines: 2 },
      log: { x: sideX, y: 640 * u, width: right - sideX, rows: LOG_ROWS },
      counters: { runs: { x: safe.x, y: 850 * u }, toGreen: { x: safe.x + 300 * u, y: 850 * u } },
    };
  }

  if (layout.orientation === "portrait") {
    return {
      header: { x: safe.x, y: 170 * u, width: safe.width, titleSize: 48 },
      at: ring({ x: safe.x + 360 * u, y: 640 * u }, 230 * u, 190 * u, 350 * u),
      node: { width: 240 * u, height: 84 * u },
      pr: { x: safe.x, y: 900 * u, width: safe.width },
      caption: { x: safe.x, y: 1050 * u, width: safe.width, lines: 3 },
      log: { x: safe.x, y: 1250 * u, width: safe.width, rows: LOG_ROWS },
      counters: { runs: { x: safe.x, y: 1600 * u }, toGreen: { x: safe.x + 480 * u, y: 1600 * u } },
    };
  }

  // square: the ring on the left, the PR and the log on the right.
  const sideX = safe.x + 580 * u;
  return {
    header: { x: safe.x, y: 150 * u, width: safe.width, titleSize: 36 },
    at: ring({ x: safe.x + 250 * u, y: 540 * u }, 160 * u, 150 * u, 200 * u),
    node: { width: 180 * u, height: 72 * u },
    pr: { x: sideX, y: 300 * u, width: right - sideX },
    log: { x: sideX, y: 450 * u, width: right - sideX, rows: LOG_ROWS },
    counters: null,
    caption: { x: safe.x, y: 790 * u, width: safe.width, lines: 2 },
  };
}

/** S6 — red CI, diagnosis, fix, green: round the loop with no human step. */
export function SelfCorrection({ trace, frame, layout, timeline }: SceneProps) {
  const state = s6State(trace, timeline, frame);
  const p = place(layout);
  const u = layout.unit;
  const green = p.at.green;
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
  const node = (id: keyof Placement["at"], at: Point) => {
    const step = state.steps.find((s) => s.id === id);
    if (!step) return null;
    return (
      <Node
        key={id}
        x={at.x}
        y={at.y}
        width={p.node.width}
        height={p.node.height}
        label={step.label}
        level={step.level}
        emphasis={step.emphasis}
        unit={u}
      />
    );
  };

  return (
    <>
      <SceneHeader
        x={p.header.x}
        y={p.header.y}
        width={p.header.width}
        eyebrow="SELF-CORRECTION"
        title={state.title}
        titleSize={p.header.titleSize}
        unit={u}
      />

      {edge("run-red", p.at.run, p.at.red, state.edges.red)}
      {edge("red-diagnosed", p.at.red, p.at.diagnosed, state.edges.diagnosed)}
      {edge("diagnosed-fixed", p.at.diagnosed, p.at.fixed, state.edges.fixed)}
      {edge("fixed-run", p.at.fixed, p.at.run, state.runs > 1 ? state.edges.run : 0)}
      {edge("run-green", p.at.run, green, state.edges.green)}

      {node("run", p.at.run)}
      {node("red", p.at.red)}
      {node("diagnosed", p.at.diagnosed)}
      {node("fixed", p.at.fixed)}
      {node("green", green)}

      {state.pr ? (
        <PrCard
          x={p.pr.x}
          y={p.pr.y}
          width={p.pr.width}
          repo={state.pr.repo}
          number={state.pr.number}
          title={state.pr.title}
          status={state.pr.status}
          level={state.pr.level}
          appear={state.pr.appear}
          titleLines={1}
          unit={u}
        />
      ) : null}

      <Feed
        x={p.log.x}
        y={p.log.y}
        width={p.log.width}
        title="THE SESSION'S LOG"
        rows={state.log}
        capacity={p.log.rows}
        empty="Waiting for CI"
        unit={u}
      />

      {p.counters ? (
        <>
          <Counter x={p.counters.runs.x} y={p.counters.runs.y} label="CI RUNS" value={String(state.runs)} unit={u} />
          <Counter
            x={p.counters.toGreen.x}
            y={p.counters.toGreen.y}
            label={state.isGreen ? "RED TO GREEN" : "SINCE FIRST RED"}
            value={state.toGreenMs === null ? "–" : formatElapsed(state.toGreenMs)}
            unit={u}
          />
        </>
      ) : null}

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
