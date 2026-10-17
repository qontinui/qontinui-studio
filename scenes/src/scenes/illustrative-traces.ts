/**
 * The committed illustrative trace of every scene (`traces/illustrative/`),
 * as untrusted JSON. The engine root uses them as its default props and the
 * embeddable package (`embed/`) ships them, so a consumer can play a scene
 * without fetching anything. A unit test checks every scene has one.
 */
import s1Illustrative from "../../traces/illustrative/s1-plan-lifecycle.json";
import s2Illustrative from "../../traces/illustrative/s2-digital-twin.json";
import s3Illustrative from "../../traces/illustrative/s3-gates-continuations.json";
import s4Illustrative from "../../traces/illustrative/s4-merge-train.json";
import s5Illustrative from "../../traces/illustrative/s5-fleet.json";
import s6Illustrative from "../../traces/illustrative/s6-self-correction.json";
import s7Illustrative from "../../traces/illustrative/s7-gui-automation.json";

export const illustrativeTraces: Readonly<Record<string, unknown>> = {
  "s1-plan-lifecycle": s1Illustrative,
  "s2-digital-twin": s2Illustrative,
  "s3-gates-continuations": s3Illustrative,
  "s4-merge-train": s4Illustrative,
  "s5-fleet": s5Illustrative,
  "s6-self-correction": s6Illustrative,
  "s7-gui-automation": s7Illustrative,
};
