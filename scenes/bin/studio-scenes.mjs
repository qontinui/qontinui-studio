#!/usr/bin/env node
// Runs the TypeScript CLI through tsx (a pinned dependency) — no build step.
import { register } from "tsx/esm/api";

register();
await import("../src/cli/main.ts");
