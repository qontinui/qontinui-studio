import { Composition, type CalculateMetadataFunction } from "remotion";

import { ASPECT_SIZES, STUDIO_FPS } from "../../api/layout";
import { sceneDuration } from "../../api/scene-root";
import { parseTrace } from "../../api/trace/schema";
import { getScene, scenes } from "../../scenes";
import { illustrativeTraces } from "../../scenes/illustrative-traces";
import { StudioComposition, type StudioCompositionProps } from "./StudioComposition";

/**
 * Default (preview) trace per scene: its committed illustrative trace. A CLI
 * render always passes its own. A unit test checks every scene has one.
 */
export const defaultTraces: Readonly<Record<string, unknown>> = illustrativeTraces;

/** Size and length come from the props, so one composition serves every aspect and trace. */
const calculateMetadata: CalculateMetadataFunction<StudioCompositionProps> = ({ props }) => {
  const { width, height } = ASPECT_SIZES[props.aspect];
  return {
    width,
    height,
    fps: STUDIO_FPS,
    durationInFrames: sceneDuration(getScene(props.sceneId), props.trace),
  };
};

export function RemotionRoot() {
  return (
    <>
      {Object.values(scenes).map((scene) => {
        const defaultTrace = defaultTraces[scene.id];
        if (defaultTrace === undefined) {
          throw new Error(`scene "${scene.id}" has no default illustrative trace registered`);
        }
        const defaultProps: StudioCompositionProps = {
          sceneId: scene.id,
          trace: parseTrace(defaultTrace),
          aspect: "16:9",
        };
        return (
          <Composition
            key={scene.id}
            id={scene.id}
            component={StudioComposition}
            defaultProps={defaultProps}
            calculateMetadata={calculateMetadata}
            fps={STUDIO_FPS}
            width={ASPECT_SIZES["16:9"].width}
            height={ASPECT_SIZES["16:9"].height}
            durationInFrames={1}
          />
        );
      })}
    </>
  );
}
