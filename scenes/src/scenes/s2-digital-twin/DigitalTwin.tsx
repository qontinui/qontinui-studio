import type { Layout, SceneProps } from "../../api";
import { Caption, Edge, Feed, Node, SceneHeader, feedHeight, type Point } from "../../primitives";
import { DECISION_ROWS, DEVELOPER_ROWS, TWIN_ROWS, s2State } from "./model";

interface Box {
  x: number;
  y: number;
  width: number;
}

interface Placement {
  header: Box & { titleSize: number };
  /** Centres of the source nodes, in trace order (up to four). */
  sources: Point[];
  sourceNode: { width: number; height: number; labelLines: number };
  /** Where each source's edge meets the twin panel. */
  sourceEdgeEnds: Point[];
  twin: Box;
  /** Rows of the twin panel (and its title's length) in this orientation. */
  twinRows: number;
  decisionRows: number;
  shortTitles: boolean;
  decisions: Box;
  developer: Box;
  /** twin → decisions and twin → developer-twin edges. */
  decisionEdge: { from: Point; to: Point };
  developerEdge: { from: Point; to: Point };
  operator: Point & { width: number; height: number; short: boolean };
  /** `lines`: the caption body's clamp, so it stays inside the safe area. */
  caption: Box & { lines: number };
}

/**
 * Where everything goes, per orientation. Pure function of the layout. The
 * header starts below the room the mode label needs at its longest, and the
 * panels start below a two-line title.
 */
function place(layout: Layout, sourceCount: number): Placement {
  const { safe, unit: u } = layout;
  const right = safe.x + safe.width;
  const n = Math.max(1, Math.min(4, sourceCount));

  if (layout.orientation === "landscape") {
    const top = 330 * u;
    const twin = { x: safe.x + 420 * u, y: top, width: 640 * u };
    const twinHeight = feedHeight(TWIN_ROWS, u);
    const sideX = twin.x + twin.width + 120 * u;
    const decisions = { x: sideX, y: top, width: right - sideX };
    const developer = { x: sideX, y: top + feedHeight(DECISION_ROWS, u) + 28 * u, width: right - sideX };
    const nodeHeight = 84 * u;
    const step = (twinHeight - nodeHeight) / Math.max(1, n - 1);
    const sources = Array.from({ length: n }, (_, i) => ({ x: safe.x + 150 * u, y: top + nodeHeight / 2 + step * i }));
    return {
      header: { x: safe.x, y: 140 * u, width: safe.width, titleSize: 48 },
      sources,
      sourceNode: { width: 300 * u, height: nodeHeight, labelLines: 2 },
      sourceEdgeEnds: sources.map((p) => ({ x: twin.x, y: p.y })),
      twin,
      twinRows: TWIN_ROWS,
      decisionRows: DECISION_ROWS,
      shortTitles: false,
      decisions,
      developer,
      decisionEdge: {
        from: { x: twin.x + twin.width, y: decisions.y + 120 * u },
        to: { x: decisions.x, y: decisions.y + 120 * u },
      },
      // Level with both panels: inside the twin and the developer twin.
      developerEdge: {
        from: { x: twin.x + twin.width, y: developer.y + 40 * u },
        to: { x: developer.x, y: developer.y + 40 * u },
      },
      operator: { x: sideX + (right - sideX) / 2, y: 915 * u, width: right - sideX, height: 96 * u, short: false },
      caption: { x: safe.x, y: top + twinHeight + 50 * u, width: twin.x + twin.width - safe.x, lines: 3 },
    };
  }

  if (layout.orientation === "portrait") {
    const nodeWidth = safe.width / n;
    const sourcesY = 410 * u;
    const sources = Array.from({ length: n }, (_, i) => ({ x: safe.x + nodeWidth * (i + 0.5), y: sourcesY }));
    const twin = { x: safe.x, y: 520 * u, width: safe.width };
    // The decisions panel leaves a lane on its right for the developer edge.
    const decisions = { x: safe.x, y: twin.y + feedHeight(TWIN_ROWS, u) + 56 * u, width: safe.width - 48 * u };
    const developer = { x: safe.x, y: decisions.y + feedHeight(DECISION_ROWS, u) + 56 * u, width: safe.width };
    const operatorY = developer.y + feedHeight(DEVELOPER_ROWS, u) + 100 * u;
    return {
      header: { x: safe.x, y: 170 * u, width: safe.width, titleSize: 48 },
      sources,
      sourceNode: { width: nodeWidth - 20 * u, height: 84 * u, labelLines: 2 },
      sourceEdgeEnds: sources.map((p) => ({ x: p.x, y: twin.y })),
      twin,
      twinRows: TWIN_ROWS,
      decisionRows: DECISION_ROWS,
      shortTitles: false,
      decisions,
      developer,
      decisionEdge: {
        from: { x: safe.x + safe.width * 0.3, y: decisions.y - 56 * u },
        to: { x: safe.x + safe.width * 0.3, y: decisions.y },
      },
      developerEdge: {
        // Down the lane right of the decisions panel, never behind it.
        from: { x: right - 20 * u, y: decisions.y - 56 * u },
        to: { x: right - 20 * u, y: developer.y },
      },
      operator: { x: safe.x + safe.width / 2, y: operatorY, width: safe.width, height: 96 * u, short: false },
      caption: { x: safe.x, y: operatorY + 110 * u, width: safe.width, lines: 3 },
    };
  }

  // square: the sources and the operator in a column on the left, each
  // source edge running straight into the twin beside them; decisions and
  // the developer twin side by side below; the caption across the bottom.
  const top = 290 * u;
  const colWidth = 210 * u;
  const nodeHeight = 50 * u;
  const nodeStep = 58 * u;
  const sources = Array.from({ length: n }, (_, i) => ({ x: safe.x + colWidth / 2, y: top + nodeHeight / 2 + nodeStep * i }));
  const twinRows = 4;
  const twin = { x: safe.x + colWidth + 30 * u, y: top, width: right - (safe.x + colWidth + 30 * u) };
  const twinBottom = top + feedHeight(twinRows, u);
  const rowY = twinBottom + 20 * u;
  const half = (safe.width - 20 * u) / 2;
  const decisions = { x: safe.x, y: rowY, width: half };
  const developer = { x: safe.x + half + 20 * u, y: rowY, width: half };
  const rowBottom = rowY + feedHeight(2, u);
  return {
    header: { x: safe.x, y: 150 * u, width: safe.width, titleSize: 36 },
    sources,
    sourceNode: { width: colWidth, height: nodeHeight, labelLines: 1 },
    sourceEdgeEnds: sources.map((p) => ({ x: twin.x, y: p.y })),
    twin,
    twinRows,
    decisionRows: 2,
    shortTitles: true,
    decisions,
    developer,
    // Short vertical edges from the twin's bottom into each panel below.
    decisionEdge: {
      from: { x: decisions.x + decisions.width * 0.75, y: twinBottom },
      to: { x: decisions.x + decisions.width * 0.75, y: rowY },
    },
    developerEdge: {
      from: { x: developer.x + developer.width * 0.5, y: twinBottom },
      to: { x: developer.x + developer.width * 0.5, y: rowY },
    },
    operator: {
      x: safe.x + colWidth / 2,
      y: twinBottom - 32 * u,
      width: colWidth,
      height: 64 * u,
      short: true,
    },
    caption: { x: safe.x, y: rowBottom + 26 * u, width: safe.width, lines: 2 },
  };
}

/** S2 — the factory's digital twin, and the developer twin that answers from served documents. */
export function DigitalTwin({ trace, frame, layout, timeline }: SceneProps) {
  const state = s2State(trace, timeline, frame);
  const p = place(layout, state.sources.length);
  const u = layout.unit;
  const facts = `${state.factCount} ${state.factCount === 1 ? "FACT" : "FACTS"}`;
  const edge = (key: string, from: Point, to: Point, pulse: number) => (
    <Edge
      key={key}
      from={from}
      to={to}
      progress={pulse}
      level={pulse > 0 ? "done" : "inert"}
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
        eyebrow="THE DIGITAL TWIN"
        title={state.title}
        titleSize={p.header.titleSize}
        unit={u}
      />

      {state.sources.slice(0, p.sources.length).map((source, i) =>
        edge(`edge-${source.id}`, p.sources[i] as Point, p.sourceEdgeEnds[i] as Point, source.pulse),
      )}
      {edge("edge-decisions", p.decisionEdge.from, p.decisionEdge.to, state.decisionPulse)}
      {edge("edge-developer", p.developerEdge.from, p.developerEdge.to, state.developerPulse)}

      {state.sources.slice(0, p.sources.length).map((source, i) => {
        const at = p.sources[i] as Point;
        return (
          <Node
            key={source.id}
            x={at.x}
            y={at.y}
            width={p.sourceNode.width}
            height={p.sourceNode.height}
            label={source.label}
            level={source.level}
            emphasis={source.emphasis}
            labelLines={p.sourceNode.labelLines}
            unit={u}
          />
        );
      })}

      <Feed
        x={p.twin.x}
        y={p.twin.y}
        width={p.twin.width}
        title={p.shortTitles ? `THE TWIN · ${facts}` : `THE TWIN IN COORD · ${facts}`}
        rows={state.twin.slice(-p.twinRows)}
        capacity={p.twinRows}
        empty="Waiting for the first report"
        unit={u}
      />
      <Feed
        x={p.decisions.x}
        y={p.decisions.y}
        width={p.decisions.width}
        title="DECISIONS FROM THE TWIN"
        rows={state.decisions.slice(-p.decisionRows)}
        capacity={p.decisionRows}
        empty="None yet"
        unit={u}
      />
      <Feed
        x={p.developer.x}
        y={p.developer.y}
        width={p.developer.width}
        title="DEVELOPER TWIN"
        rows={state.developer}
        capacity={DEVELOPER_ROWS}
        empty="No questions yet"
        unit={u}
      />

      <Node
        x={p.operator.x}
        y={p.operator.y}
        width={p.operator.width}
        height={p.operator.height}
        label="Operator"
        sublabel={p.operator.short ? state.operator.short : state.operator.sublabel}
        level={state.operator.level}
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
