import { defineConfig } from "vitest/config";

// The Phase 3 scene matrix: every scene x aspect renders, embeds in the test
// page, and shows its D2 mode label on screen; reduced motion is honoured.
// Needs the pinned headless browser (`pnpm browser:ensure`). CI runs one job
// per aspect via SCENE_MATRIX_ASPECTS.
export default defineConfig({
  test: {
    include: ["tests/matrix/**/*.test.ts"],
    environment: "node",
    testTimeout: 600_000,
    hookTimeout: 600_000,
    fileParallelism: false,
  },
});
