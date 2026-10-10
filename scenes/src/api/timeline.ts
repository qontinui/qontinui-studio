/**
 * Trace → timeline. Real time between factory events ranges from seconds to
 * days, so a scene does not play it to scale: each event becomes a BEAT at a
 * fixed animation interval, in trace order. The real timestamps travel with
 * the beats, so a scene can still show real elapsed time (the counter) while
 * the animation stays readable.
 *
 * Ordering is total and stable — by timestamp, then by position in the trace —
 * so the same trace always yields the same timeline.
 */
import { interpolate, msToFrames } from "./motion";
import type { EventKind, SceneTrace, TraceEvent } from "./trace/schema";

export interface TimelineOptions {
  fps: number;
  /** Hold before the first beat. */
  introMs: number;
  /** Interval between consecutive beats. */
  beatMs: number;
  /** Hold after the last beat. */
  outroMs: number;
  /** Only these kinds become beats (default: every event). */
  kinds?: readonly EventKind[];
}

export interface Beat {
  index: number;
  event: TraceEvent;
  startFrame: number;
  /** Event time, ms since the epoch. */
  atMs: number;
}

export interface Timeline {
  fps: number;
  durationInFrames: number;
  beatFrames: number;
  beats: readonly Beat[];
}

/** An event's timestamp as ms since the epoch. */
export function eventTimeMs(event: TraceEvent): number {
  return Date.parse(event.at);
}

export function sortEvents(events: readonly TraceEvent[]): TraceEvent[] {
  return events
    .map((event, order) => ({ event, order, at: Date.parse(event.at) }))
    .sort((a, b) => a.at - b.at || a.order - b.order)
    .map((x) => x.event);
}

export function buildTimeline(trace: SceneTrace, options: TimelineOptions): Timeline {
  const { fps } = options;
  const intro = msToFrames(options.introMs, fps);
  const beatFrames = Math.max(1, msToFrames(options.beatMs, fps));
  const outro = msToFrames(options.outroMs, fps);
  const wanted = options.kinds ? new Set<EventKind>(options.kinds) : null;

  const beats = sortEvents(trace.events)
    .filter((event) => wanted === null || wanted.has(event.kind))
    .map((event, index) => ({
      index,
      event,
      startFrame: intro + index * beatFrames,
      atMs: Date.parse(event.at),
    }));

  const lastStart = beats.length ? (beats[beats.length - 1] as Beat).startFrame : intro;
  return { fps, beatFrames, beats, durationInFrames: lastStart + beatFrames + outro };
}

/** Index of the latest beat that has started at `frame`; -1 before the first. */
export function activeBeatIndex(timeline: Timeline, frame: number): number {
  let active = -1;
  for (const beat of timeline.beats) {
    if (beat.startFrame <= frame) active = beat.index;
    else break;
  }
  return active;
}

/** The first beat for an event matching `predicate`, if any. */
export function findBeat(
  timeline: Timeline,
  predicate: (event: TraceEvent) => boolean,
): Beat | undefined {
  return timeline.beats.find((beat) => predicate(beat.event));
}

/**
 * Real time (ms since the epoch) shown at `frame`: held at the first event
 * before it, interpolated between beats, held at the last event after it.
 */
export function realTimeAt(timeline: Timeline, frame: number): number {
  const { beats } = timeline;
  if (beats.length === 0) return 0;
  if (beats.length === 1) return (beats[0] as Beat).atMs;
  return interpolate(
    frame,
    beats.map((b) => b.startFrame),
    beats.map((b) => b.atMs),
  );
}
