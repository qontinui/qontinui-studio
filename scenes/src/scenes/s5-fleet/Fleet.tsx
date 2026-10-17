import type { Layout, SceneProps } from "../../api";
import { Caption, Counter, Feed, SceneHeader, feedHeight } from "../../primitives";
import { LOG_ROWS, MAX_MACHINES, SESSIONS_PER_MACHINE, s5State } from "./model";

interface Box {
  x: number;
  y: number;
  width: number;
}

interface Placement {
  header: Box & { titleSize: number };
  /** One panel per machine, in trace order. */
  machines: Box[];
  log: Box & { rows: number };
  running: { x: number; y: number };
  avoided: { x: number; y: number };
  caption: Box & { lines: number };
}

/** Where everything goes, per orientation. Pure function of the layout. */
function place(layout: Layout): Placement {
  const { safe, unit: u } = layout;
  const right = safe.x + safe.width;
  const machineH = feedHeight(SESSIONS_PER_MACHINE, u);

  if (layout.orientation === "landscape") {
    const gap = 24 * u;
    const w = (safe.width - gap * (MAX_MACHINES - 1)) / MAX_MACHINES;
    const top = 320 * u;
    const logY = top + machineH + 36 * u;
    const sideX = safe.x + 1140 * u;
    return {
      header: { x: safe.x, y: 140 * u, width: safe.width, titleSize: 48 },
      machines: Array.from({ length: MAX_MACHINES }, (_, i) => ({ x: safe.x + i * (w + gap), y: top, width: w })),
      log: { x: safe.x, y: logY, width: 1100 * u, rows: LOG_ROWS },
      running: { x: sideX, y: logY },
      avoided: { x: sideX + 340 * u, y: logY },
      caption: { x: sideX, y: logY + 130 * u, width: right - sideX, lines: 3 },
    };
  }

  if (layout.orientation === "portrait") {
    const top = 380 * u;
    const gap = 20 * u;
    const machines = Array.from({ length: MAX_MACHINES }, (_, i) => ({ x: safe.x, y: top + i * (machineH + gap), width: safe.width }));
    const logY = top + MAX_MACHINES * (machineH + gap) + 16 * u;
    const below = logY + feedHeight(LOG_ROWS, u) + 36 * u;
    return {
      header: { x: safe.x, y: 170 * u, width: safe.width, titleSize: 48 },
      machines,
      log: { x: safe.x, y: logY, width: safe.width, rows: LOG_ROWS },
      running: { x: safe.x, y: below },
      avoided: { x: safe.x + 480 * u, y: below },
      caption: { x: safe.x, y: below + 130 * u, width: safe.width, lines: 3 },
    };
  }

  // square
  const gap = 16 * u;
  const w = (safe.width - gap * (MAX_MACHINES - 1)) / MAX_MACHINES;
  const top = 290 * u;
  const logY = top + machineH + 24 * u;
  const logRows = 4;
  const sideX = safe.x + 640 * u;
  return {
    header: { x: safe.x, y: 150 * u, width: safe.width, titleSize: 36 },
    machines: Array.from({ length: MAX_MACHINES }, (_, i) => ({ x: safe.x + i * (w + gap), y: top, width: w })),
    log: { x: safe.x, y: logY, width: 616 * u, rows: logRows },
    running: { x: sideX, y: logY },
    avoided: { x: sideX, y: logY + 130 * u },
    caption: { x: safe.x, y: logY + feedHeight(logRows, u) + 24 * u, width: safe.width, lines: 2 },
  };
}

/** S5 — sessions on several machines, each in its own worktree, coordinating through intent and claims. */
export function Fleet({ trace, frame, layout, timeline }: SceneProps) {
  const state = s5State(trace, timeline, frame);
  const p = place(layout);
  const u = layout.unit;

  return (
    <>
      <SceneHeader
        x={p.header.x}
        y={p.header.y}
        width={p.header.width}
        eyebrow="THE FLEET"
        title={state.title}
        titleSize={p.header.titleSize}
        unit={u}
      />

      {state.machines.map((machine, i) => {
        const box = p.machines[i];
        if (!box) return null;
        return (
          <Feed
            key={machine.id}
            x={box.x}
            y={box.y}
            width={box.width}
            title={machine.label.toUpperCase()}
            rows={machine.sessions}
            capacity={SESSIONS_PER_MACHINE}
            empty="idle"
            unit={u}
          />
        );
      })}

      <Feed
        x={p.log.x}
        y={p.log.y}
        width={p.log.width}
        title="COORD · INTENT AND CLAIMS"
        rows={state.log.slice(-p.log.rows)}
        capacity={p.log.rows}
        empty="Nothing declared yet"
        unit={u}
      />

      <Counter x={p.running.x} y={p.running.y} label="SESSIONS RUNNING" value={String(state.running)} unit={u} />
      <Counter x={p.avoided.x} y={p.avoided.y} label="COLLISIONS AVOIDED" value={String(state.avoided)} unit={u} />

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
