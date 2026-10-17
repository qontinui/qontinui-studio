/** Composition registry: every composition the CLI can compose, by id. */
import type { CompositionDefinition } from "../api/composition";
import { explainer } from "./explainer";
import { factoryPulse } from "./factory-pulse";
import { teaser } from "./teaser";

const ALL: readonly CompositionDefinition[] = [teaser, explainer, factoryPulse];

export const compositions: Readonly<Record<string, CompositionDefinition>> = Object.fromEntries(
  ALL.map((composition) => [composition.id, composition]),
);

export function getComposition(id: string): CompositionDefinition {
  const composition = compositions[id];
  if (!composition) {
    throw new Error(`unknown composition "${id}" (known: ${Object.keys(compositions).join(", ")})`);
  }
  return composition;
}
