/**
 * What every adapter renders for a composition: the program's frame. A scene
 * segment is drawn by `SceneRoot` exactly as a lone scene render draws it (so
 * its plan-D2 mode label comes with it); a card or a b-roll still is drawn
 * here, with the mode label of its nearest scene (a card carries no data, but
 * every frame of an output states its mode).
 *
 * Pure: a function of `(program, frame)` and the adapter's image component.
 */
import { BrollStill, type ImageComponent } from "../primitives/BrollStill";
import { ModeLabel } from "../primitives/ModeLabel";
import { TitleCard } from "../primitives/TitleCard";
import { programFrameAt, type Program } from "./composition";
import { layoutFor, type Aspect } from "./layout";
import type { SceneDefinition } from "./scene";
import { SceneRoot } from "./scene-root";
import { surface } from "./tokens";
import { fontStack } from "./typography";

export interface ProgramRootProps {
  program: Program;
  frame: number;
  aspect: Aspect;
  /** Scene registry, by id. */
  scenes: Readonly<Record<string, SceneDefinition>>;
  Image: ImageComponent;
}

export function ProgramRoot({ program, frame, aspect, scenes, Image }: ProgramRootProps) {
  const at = programFrameAt(program, frame);
  const segment = at.segment;
  if (segment.kind === "scene") {
    const scene = scenes[segment.sceneId];
    if (!scene) throw new Error(`program "${program.compositionId}": unknown scene "${segment.sceneId}"`);
    return (
      <div data-studio-segment={segment.id} data-studio-program={program.compositionId}>
        <SceneRoot scene={scene} trace={segment.trace} frame={"sceneFrame" in at ? at.sceneFrame : 0} aspect={aspect} />
      </div>
    );
  }
  const layout = layoutFor(aspect);
  return (
    <div data-studio-segment={segment.id} data-studio-program={program.compositionId}>
      <div
        data-studio-aspect={aspect}
        style={{
          position: "relative",
          width: layout.width,
          height: layout.height,
          overflow: "hidden",
          background: surface.canvas,
          fontFamily: fontStack,
        }}
      >
        <div style={{ position: "absolute", inset: 0, isolation: "isolate", zIndex: 0 }}>
          {segment.kind === "card" ? (
            <TitleCard
              title={segment.title}
              {...(segment.body === undefined ? {} : { body: segment.body })}
              layout={layout}
              local={at.local}
              fps={program.fps}
            />
          ) : (
            <BrollStill
              src={segment.src}
              {...(segment.caption === undefined ? {} : { caption: segment.caption })}
              layout={layout}
              Image={Image}
            />
          )}
        </div>
        <div style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none" }}>
          <ModeLabel meta={segment.meta} layout={layout} />
        </div>
      </div>
    </div>
  );
}
