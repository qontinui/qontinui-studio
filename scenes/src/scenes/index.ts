/** Scene registry: every scene the CLI and adapters can render, by id. */
import type { SceneDefinition } from "../api";
import { s1PlanLifecycle } from "./s1-plan-lifecycle";

export const scenes: Readonly<Record<string, SceneDefinition>> = {
  [s1PlanLifecycle.id]: s1PlanLifecycle,
};

export function getScene(id: string): SceneDefinition {
  const scene = scenes[id];
  if (!scene) {
    throw new Error(`unknown scene "${id}" (known: ${Object.keys(scenes).join(", ")})`);
  }
  return scene;
}
