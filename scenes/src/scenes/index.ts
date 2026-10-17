/** Scene registry: every scene the CLI and adapters can render, by id. */
import type { SceneDefinition } from "../api";
import { s1PlanLifecycle } from "./s1-plan-lifecycle";
import { s2DigitalTwin } from "./s2-digital-twin";
import { s3GatesContinuations } from "./s3-gates-continuations";
import { s4MergeTrain } from "./s4-merge-train";
import { s5Fleet } from "./s5-fleet";
import { s6SelfCorrection } from "./s6-self-correction";

const ALL: readonly SceneDefinition[] = [s1PlanLifecycle, s2DigitalTwin, s3GatesContinuations, s4MergeTrain, s5Fleet, s6SelfCorrection];

export const scenes: Readonly<Record<string, SceneDefinition>> = Object.fromEntries(
  ALL.map((scene) => [scene.id, scene]),
);

export function getScene(id: string): SceneDefinition {
  const scene = scenes[id];
  if (!scene) {
    throw new Error(`unknown scene "${id}" (known: ${Object.keys(scenes).join(", ")})`);
  }
  return scene;
}
