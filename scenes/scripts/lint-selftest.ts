/**
 * Lint self-test: proves the engine-seam and scene-purity rules REFUSE, not
 * merely that no violation exists today.
 *
 * Each file in lint-fixtures/<area>/ starts with `// expect: <rule>[, <rule>]`
 * or `// expect: none`. It is linted as if it lived at
 * src/<area>/__lint_selftest__/<name>, so the real config's file globs apply.
 * The test fails when an expected rule does not fire, when a `none` fixture
 * reports anything, or when the virtual path is ignored by the config.
 */
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FIXTURES = path.join(ROOT, "lint-fixtures");

const eslint = new ESLint({ cwd: ROOT });
let failures = 0;
let checked = 0;

for (const area of (await readdir(FIXTURES)).sort()) {
  for (const name of (await readdir(path.join(FIXTURES, area))).sort()) {
    const source = await readFile(path.join(FIXTURES, area, name), "utf8");
    const header = /^\/\/ expect: (.+)$/m.exec(source.split("\n")[0] ?? "");
    const label = `${area}/${name}`;
    if (!header) {
      console.log(`FAIL ${label}: missing "// expect:" header`);
      failures++;
      continue;
    }
    const expected = header[1] === "none" ? [] : (header[1] as string).split(",").map((r) => r.trim());
    const filePath = path.join(ROOT, "src", area, "__lint_selftest__", name);
    if (await eslint.isPathIgnored(filePath)) {
      console.log(`FAIL ${label}: virtual path ${path.relative(ROOT, filePath)} is ignored by the config`);
      failures++;
      continue;
    }
    const [result] = await eslint.lintText(source, { filePath });
    const errors = (result?.messages ?? []).filter((m) => m.severity === 2);
    const fired = new Set(errors.map((m) => m.ruleId ?? "(fatal)"));
    checked++;

    const missing = expected.filter((rule) => !fired.has(rule));
    const unexpectedInClean = expected.length === 0 && errors.length > 0;
    if (missing.length || unexpectedInClean || (expected.length > 0 && errors.length === 0)) {
      failures++;
      console.log(`FAIL ${label}: expected [${expected.join(", ") || "none"}], got [${[...fired].join(", ") || "none"}]`);
      for (const m of errors) console.log(`       ${m.line}:${m.column} ${m.ruleId}: ${m.message}`);
    } else {
      console.log(`ok   ${label}: ${expected.length ? [...fired].join(", ") : "clean, as expected"}`);
    }
  }
}

console.log(`\nlint self-test: ${checked - failures}/${checked} fixtures behaved as expected`);
if (failures > 0 || checked === 0) process.exit(1);
