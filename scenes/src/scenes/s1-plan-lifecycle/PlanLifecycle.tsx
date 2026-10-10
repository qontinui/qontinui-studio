import { formatDate, formatElapsed, text, typeScale, fontStack, fontWeights, type Layout, type SceneProps } from "../../api";
import { Caption, Counter, Edge, Gate, Node, PrCard, SessionLane, Timeline, type Point } from "../../primitives";
import { STAGES, s1State } from "./model";

interface Placement {
  /** `titleSize` in reference px; the title is clamped to two lines. */
  header: { x: number; y: number; width: number; titleSize: number };
  nodes: Point[];
  node: { width: number; height: number };
  caption: { x: number; y: number; width: number };
  pr: { x: number; y: number; width: number };
  gate: { x: number; y: number; size: number };
  counter: { x: number; y: number };
  lanes: { x: number; y: number; width: number; step: number };
  axis: { x: number; y: number; width: number };
}

/**
 * Where everything goes, per orientation. Pure function of the layout. The
 * header starts below the room the mode label needs at its longest (clamped
 * to three lines), and the stages start below a two-line (clamped) title.
 */
function place(layout: Layout): Placement {
  const { safe, unit: u } = layout;
  const right = safe.x + safe.width;

  if (layout.orientation === "landscape") {
    const step = safe.width / STAGES.length;
    return {
      header: { x: safe.x, y: 140 * u, width: safe.width, titleSize: 48 },
      nodes: STAGES.map((_, i) => ({ x: safe.x + step * (i + 0.5), y: 355 * u })),
      node: { width: step - 30 * u, height: 104 * u },
      caption: { x: safe.x, y: 480 * u, width: 860 * u },
      pr: { x: right - 620 * u - 250 * u, y: 470 * u, width: 620 * u },
      gate: { x: right - 120 * u, y: 530 * u, size: 110 * u },
      counter: { x: safe.x, y: 650 * u },
      lanes: { x: safe.x, y: 790 * u, width: safe.width, step: 62 * u },
      axis: { x: safe.x, y: 960 * u, width: safe.width },
    };
  }

  if (layout.orientation === "portrait") {
    const colWidth = 400 * u;
    const top = 384 * u;
    const step = 118 * u;
    const rightX = safe.x + colWidth + 48 * u;
    const rightW = right - rightX;
    return {
      header: { x: safe.x, y: 170 * u, width: safe.width, titleSize: 48 },
      nodes: STAGES.map((_, i) => ({ x: safe.x + colWidth / 2, y: top + step * i })),
      node: { width: colWidth, height: 92 * u },
      caption: { x: rightX, y: top - 40 * u, width: rightW },
      pr: { x: rightX, y: top + 220 * u, width: rightW },
      gate: { x: rightX + rightW / 2, y: top + 520 * u, size: 110 * u },
      counter: { x: safe.x, y: 1310 * u },
      lanes: { x: safe.x, y: 1470 * u, width: safe.width, step: 70 * u },
      axis: { x: safe.x, y: 1720 * u, width: safe.width },
    };
  }

  // square: two rows of four, snaking so every edge is short.
  const perRow = 4;
  const step = safe.width / perRow;
  const cardWidth = 620 * u;
  const sideX = safe.x + 300 * u;
  return {
    header: { x: safe.x, y: 150 * u, width: safe.width, titleSize: 36 },
    nodes: STAGES.map((_, i) => {
      const row = Math.floor(i / perRow);
      const col = row === 0 ? i : perRow - 1 - (i - perRow);
      return { x: safe.x + step * (col + 0.5), y: (330 + row * 124) * u };
    }),
    node: { width: step - 26 * u, height: 84 * u },
    caption: { x: safe.x, y: 528 * u, width: safe.width },
    pr: { x: safe.x, y: 652 * u, width: cardWidth },
    gate: { x: safe.x + cardWidth + (safe.width - cardWidth) / 2, y: 687 * u, size: 80 * u },
    counter: { x: safe.x, y: 816 * u },
    lanes: { x: sideX, y: 808 * u, width: right - sideX, step: 58 * u },
    axis: { x: safe.x, y: 970 * u, width: safe.width },
  };
}

/** S1 — a plan travels draft → vetted → implementing → PR → CI → merge train → landed → shipped. */
export function PlanLifecycle({ trace, frame, layout, timeline }: SceneProps) {
  const state = s1State(trace, timeline, frame);
  const p = place(layout);
  const u = layout.unit;

  return (
    <>
      <div
        style={{
          position: "absolute",
          left: p.header.x,
          top: p.header.y,
          width: p.header.width,
          fontFamily: fontStack,
        }}
      >
        <div
          style={{
            color: text.muted,
            fontSize: Math.round(typeScale.label * u),
            fontWeight: fontWeights.semibold,
            letterSpacing: "0.12em",
          }}
        >
          PLAN LIFECYCLE
        </div>
        <div
          style={{
            color: text.primary,
            fontSize: Math.round(p.header.titleSize * u),
            fontWeight: fontWeights.bold,
            lineHeight: 1.1,
            marginTop: Math.round(8 * u),
            // At most two lines, whatever the title length (schema max 160);
            // the layout below reserves exactly that much room.
            display: "-webkit-box",
            WebkitBoxOrient: "vertical",
            WebkitLineClamp: 2,
            overflow: "hidden",
            overflowWrap: "anywhere",
          }}
        >
          {state.plan.title}
        </div>
      </div>

      {state.edges.map((edge, i) => (
        <Edge
          key={`edge-${i}`}
          from={p.nodes[i] as Point}
          to={p.nodes[i + 1] as Point}
          progress={edge.progress}
          level={edge.level}
          unit={u}
          frameWidth={layout.width}
          frameHeight={layout.height}
        />
      ))}

      {state.stages.map((stage, i) => {
        const at = p.nodes[i] as Point;
        return (
          <Node
            key={stage.label}
            x={at.x}
            y={at.y}
            width={p.node.width}
            height={p.node.height}
            label={stage.label}
            level={stage.level}
            appear={stage.appear}
            emphasis={stage.emphasis}
            unit={u}
          />
        );
      })}

      {state.caption ? (
        <Caption
          x={p.caption.x}
          y={p.caption.y}
          width={p.caption.width}
          title={state.caption.title}
          body={state.caption.body}
          appear={state.caption.appear}
          unit={u}
        />
      ) : null}

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

      <Counter x={p.counter.x} y={p.counter.y} label="ELAPSED SINCE DRAFT" value={formatElapsed(state.elapsedMs)} unit={u} />

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
