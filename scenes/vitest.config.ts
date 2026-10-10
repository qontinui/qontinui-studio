import { defineConfig } from "vitest/config";

// Unit tests: pure TypeScript, no browser. The golden-frame suite has its own
// config (vitest.golden.config.ts) because it bundles and drives Chromium.
export default defineConfig({
  test: {
    include: ["tests/unit/**/*.test.ts"],
    environment: "node",
  },
});
