/**
 * The Remotion composition for a studio program (plan Phase 5): maps
 * Remotion's frame clock onto `ProgramRoot`, serves the program's b-roll
 * stills through Remotion's `Img` (the frame waits until the image is
 * decoded) and places each narration clip with `Html5Audio` at the frame the
 * program gives it. Remotion mixes the audio itself when it encodes.
 */
import { useEffect, useState } from "react";
import {
  Html5Audio,
  Img,
  Sequence,
  cancelRender,
  continueRender,
  delayRender,
  staticFile,
  useCurrentFrame,
} from "remotion";

import type { Program } from "../../api/composition";
import type { Aspect } from "../../api/layout";
import { ProgramRoot } from "../../api/program-root";
import { scenes } from "../../scenes";
import { loadStudioFonts } from "../fonts";

export type StudioProgramProps = {
  program: Program;
  aspect: Aspect;
};

function StaticImage({ src, style }: { src: string; style: Record<string, string | number> }) {
  return <Img src={staticFile(src)} style={style} />;
}

export function StudioProgram({ program, aspect }: StudioProgramProps) {
  const frame = useCurrentFrame();
  const [fontHandle] = useState(() => delayRender("Loading the studio fonts"));
  useEffect(() => {
    loadStudioFonts("remotion-static", staticFile)
      .then(() => continueRender(fontHandle))
      .catch((err: unknown) => cancelRender(err instanceof Error ? err : new Error(String(err))));
  }, [fontHandle]);

  return (
    <>
      <ProgramRoot program={program} frame={frame} aspect={aspect} scenes={scenes} Image={StaticImage} />
      {program.audio.map((clip) => (
        <Sequence key={clip.segmentId} from={clip.startFrame} durationInFrames={clip.durationInFrames} layout="none">
          <Html5Audio src={staticFile(clip.src)} />
        </Sequence>
      ))}
    </>
  );
}
