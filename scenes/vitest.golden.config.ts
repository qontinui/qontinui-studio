import { defineConfig } from "vitest/config";

// Golden-frame and determinism suite: bundles the scenes and renders stills
// through the engine adapter, so it is slow and needs the pinned headless
// browser (`pnpm browser:ensure`).
export default defineConfig({
  test: {
    include: ["tests/golden/**/*.test.ts"],
    environment: "node",
    testTimeout: 300_000,
    hookTimeout: 300_000,
    fileParallelism: false,
  },
});
