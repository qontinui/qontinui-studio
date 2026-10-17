import { Composition, type CalculateMetadataFunction } from "remotion";

import { ASPECT_SIZES, STUDIO_FPS } from "../../api/layout";
import { sceneDuration } from "../../api/scene-root";
import { parseTrace } from "../../api/trace/schema";
import { getScene, scenes } from "../../scenes";
import s1Illustrative from "../../../traces/illustrative/s1-plan-lifecycle.json";
import s2Illustrative from "../../../traces/illustrative/s2-digital-twin.json";
import s3Illustrative from "../../../traces/illustrative/s3-gates-continuations.json";
import s4Illustrative from "../../../traces/illustrative/s4-merge-train.json";
import s5Illustrative from "../../../traces/illustrative/s5-fleet.json";
import s6Illustrative from "../../../traces/illustrative/s6-self-correction.json";
import s7Illustrative from "../../../traces/illustrative/s7-gui-automation.json";
import { StudioComposition, type StudioCompositionProps } from "./StudioComposition";

/**
 * Default (preview) trace per scene: its committed illustrative trace. A CLI
 * render always passes its own. A unit test checks every scene has one.
 */
export const defaultTraces: Record<string, unknown> = {
  "s1-plan-lifecycle": s1Illustrative,
  "s2-digital-twin": s2Illustrative,
  "s3-gates-continuations": s3Illustrative,
  "s4-merge-train": s4Illustrative,
  "s5-fleet": s5Illustrative,
  "s6-self-correction": s6Illustrative,
  "s7-gui-automation": s7Illustrative,
};

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
