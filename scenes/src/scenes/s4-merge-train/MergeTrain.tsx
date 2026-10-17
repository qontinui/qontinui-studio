import type { Layout, SceneProps } from "../../api";
import { Caption, Counter, Feed, PrCard, SceneHeader, feedHeight } from "../../primitives";
import { TRAIN_SLOTS, s4State } from "./model";

interface Box {
  x: number;
  y: number;
  width: number;
}

interface Placement {
  header: Box & { titleSize: number };
  /** The train panel; cars stack inside it, one slot apart. */
  train: Box & { capacity: number };
  car: { x: number; y: number; width: number; step: number; asideDx: number; exitDx: number };
  main: Box;
  counter: { x: number; y: number };
  caption: Box & { lines: number };
}

/** Feed rows of room that hold `slots` cars `step` apart (Feed rows are 58 reference px). */
function trainCapacity(slots: number, step: number, u: number): number {
  return Math.ceil((slots * step) / (58 * u));
}

/** Where everything goes, per orientation. Pure function of the layout. */
function place(layout: Layout): Placement {
  const { safe, unit: u } = layout;
  const right = safe.x + safe.width;
  const step = 116 * u;
  const capacity = trainCapacity(TRAIN_SLOTS, step, u);
  const inner = (box: Box) => ({ x: box.x + 16 * u, y: box.y + 16 * u + 36 * u, width: box.width - 32 * u });

  if (layout.orientation === "landscape") {
    const train = { x: safe.x, y: 320 * u, width: 900 * u };
    const sideX = train.x + train.width + 40 * u;
    const main = { x: sideX, y: 320 * u, width: right - sideX };
    const i = inner(train);
    return {
      header: { x: safe.x, y: 140 * u, width: safe.width, titleSize: 48 },
      train: { ...train, capacity },
      car: { x: i.x, y: i.y, width: i.width - 48 * u, step, asideDx: 48 * u, exitDx: 48 * u },
      main,
      counter: { x: sideX, y: main.y + feedHeight(TRAIN_SLOTS, u) + 40 * u },
      caption: { x: sideX, y: main.y + feedHeight(TRAIN_SLOTS, u) + 170 * u, width: main.width, lines: 2 },
    };
  }

  if (layout.orientation === "portrait") {
    const train = { x: safe.x, y: 380 * u, width: safe.width };
    const i = inner(train);
    const mainY = train.y + feedHeight(capacity, u) + 36 * u;
    const main = { x: safe.x, y: mainY, width: safe.width };
    const below = mainY + feedHeight(TRAIN_SLOTS, u) + 40 * u;
    return {
      header: { x: safe.x, y: 170 * u, width: safe.width, titleSize: 48 },
      train: { ...train, capacity },
      car: { x: i.x, y: i.y, width: i.width - 48 * u, step, asideDx: 48 * u, exitDx: 48 * u },
      main,
      counter: { x: safe.x, y: below },
      caption: { x: safe.x, y: below + 130 * u, width: safe.width, lines: 3 },
    };
  }

  // square: the train on the left, main and the caption on the right.
  const train = { x: safe.x, y: 290 * u, width: 520 * u };
  const i = inner(train);
  const sideX = train.x + train.width + 24 * u;
  const main = { x: sideX, y: 290 * u, width: right - sideX };
  return {
    header: { x: safe.x, y: 150 * u, width: safe.width, titleSize: 36 },
    train: { ...train, capacity },
    car: { x: i.x, y: i.y, width: i.width - 24 * u, step, asideDx: 24 * u, exitDx: 24 * u },
    main,
    counter: { x: sideX, y: main.y + feedHeight(TRAIN_SLOTS, u) + 24 * u },
    caption: { x: sideX, y: main.y + feedHeight(TRAIN_SLOTS, u) + 140 * u, width: main.width, lines: 3 },
  };
}

/** S4 — PRs queued, rebased, tested and landed in order; a conflict steps out and rejoins. */
export function MergeTrain({ trace, frame, layout, timeline }: SceneProps) {
  const state = s4State(trace, timeline, frame);
  const p = place(layout);
  const u = layout.unit;

  return (
    <>
      <SceneHeader
        x={p.header.x}
        y={p.header.y}
        width={p.header.width}
        eyebrow="THE MERGE TRAIN"
        title={state.title}
        titleSize={p.header.titleSize}
        unit={u}
      />

      <Feed x={p.train.x} y={p.train.y} width={p.train.width} title="IN THE TRAIN · HEAD FIRST" rows={[]} capacity={p.train.capacity} unit={u} />
      {state.cars.map((car) => (
        <PrCard
          key={car.id}
          x={p.car.x + car.aside * p.car.asideDx + car.exit * p.car.exitDx}
          y={p.car.y + car.slot * p.car.step}
          width={p.car.width}
          repo={car.repo}
          number={car.number}
          title={car.title}
          status={car.status}
          level={car.level}
          appear={Math.min(car.appear, 1 - car.exit)}
          titleLines={1}
          unit={u}
        />
      ))}

      <Feed
        x={p.main.x}
        y={p.main.y}
        width={p.main.width}
        title="MAIN · LANDED"
        rows={state.main.slice(-TRAIN_SLOTS).map((r) => (layout.orientation === "square" ? { ...r, detail: r.short } : r))}
        capacity={TRAIN_SLOTS}
        empty="Nothing landed yet"
        unit={u}
      />

      <Counter x={p.counter.x} y={p.counter.y} label="LANDED" value={`${state.landed} of ${state.total}`} unit={u} />

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
