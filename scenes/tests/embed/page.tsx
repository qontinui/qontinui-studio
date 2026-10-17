/**
 * The embed test page (plan Phase 3: every scene "embeds in a test page").
 * A browser entry, bundled by `./serve.ts` and driven by the scene matrix
 * (`tests/matrix/scene-matrix.test.ts`); `pnpm embed:serve` opens it for a
 * human.
 *
 * It mounts the adapter's web player exactly as a consumer would, with a
 * scene's committed illustrative trace fetched as untrusted JSON:
 *
 *   /?scene=<id>&aspect=16:9|9:16|1:1&mode=illustrative|replay&autoplay=0|1&controls=0|1
 *
 * `mode=replay` turns the same events into a REPLAY trace attested through
 * the test-only hook, so the matrix can check the replay label (date range
 * and source) on screen. The bundle defines `process.env.VITEST` for that
 * hook; nothing outside tests/ builds or serves this page.
 */
import { createRoot } from "react-dom/client";

import { StudioPlayer } from "../../src/adapters/remotion/Player";
import { ASPECT_SIZES, isAspect } from "../../src/api/layout";
import { parseTrace } from "../../src/api/trace/schema";
import { attestReplayForTests } from "../../src/api/trace/testing";
import { embedReplayMeta } from "./replay-meta";

/** CSS width of the player per aspect, so every aspect fits one viewport. */
const PLAYER_WIDTH: Record<keyof typeof ASPECT_SIZES, number> = { "16:9": 960, "9:16": 450, "1:1": 720 };

async function main() {
  const params = new URLSearchParams(window.location.search);
  const scene = params.get("scene") ?? "s1-plan-lifecycle";
  const aspect = params.get("aspect") ?? "16:9";
  if (!isAspect(aspect)) throw new Error(`unknown aspect ${aspect}`);
  const mode = params.get("mode") ?? "illustrative";
  const autoPlay = params.get("autoplay") === "1";
  const controls = params.get("controls") !== "0";

  const response = await fetch(`/traces/${encodeURIComponent(scene)}.json`);
  if (!response.ok) throw new Error(`no illustrative trace for ${scene} (${response.status})`);
  const json: unknown = await response.json();
  let trace: unknown = json;
  if (mode === "replay") {
    const illustrative = parseTrace(json);
    const meta = embedReplayMeta(illustrative.meta.title, illustrative.events);
    trace = attestReplayForTests(parseTrace({ ...illustrative, meta }));
  }

  const container = document.getElementById("root") as HTMLElement;
  container.style.width = `${PLAYER_WIDTH[aspect]}px`;
  createRoot(container).render(
    <StudioPlayer scene={scene} trace={trace} aspect={aspect} assetBaseUrl="/assets" autoPlay={autoPlay} controls={controls} />,
  );
  document.body.dataset.embedReady = "1";
}

main().catch((err: unknown) => {
  document.body.dataset.embedError = err instanceof Error ? err.message : String(err);
});
