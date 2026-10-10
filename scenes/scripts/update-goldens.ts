/**
 * Re-render the S1 golden frames into tests/golden/__goldens__/.
 * Run after an INTENDED visual change, inspect the PNGs, and commit them.
 */
import { rmSync } from "node:fs";

import { RemotionAdapter } from "../src/adapters/remotion/render";
import { GOLDEN_CASES, GOLDEN_DIR, GOLDEN_SCALE, SCENE_ID, casePrefix, loadS1Trace } from "../tests/golden/cases";

rmSync(GOLDEN_DIR, { recursive: true, force: true });
const adapter = new RemotionAdapter();
try {
  const trace = loadS1Trace();
  for (const { aspect, frames } of GOLDEN_CASES) {
    const files = await adapter.renderStills({
      scene: SCENE_ID,
      trace,
      aspect,
      frames,
      outDir: GOLDEN_DIR,
      prefix: casePrefix(aspect),
      scale: GOLDEN_SCALE,
    });
    for (const file of files) process.stdout.write(`${file}\n`);
  }
} finally {
  await adapter.close();
}
