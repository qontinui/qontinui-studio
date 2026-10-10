/**
 * Lint self-test: proves the engine-seam, provenance and scene-purity rules
 * REFUSE, not merely that no violation exists today.
 *
 * Each file in lint-fixtures/<area>/ starts with header comment lines:
 *
 *   // expect: <rule-id> :: <message substring>     (one line per expected error)
 *   // expect: none                                 (a clean control)
 *   // path: <virtual path>                         (optional)
 *
 * The file is linted as if it lived at <virtual path>, by default
 * src/<area>/__lint_selftest__/<name> (or <area>/__lint_selftest__/<name> for
 * the top-level areas tests/ and scripts/), so the real config's globs apply.
 *
 * A fixture passes only when every expectation is met by an error with that
 * rule id whose message contains the substring, AND every error is accounted
 * for by an expectation. A missing header or an ignored virtual path is a
 * failure, and every fixture is counted in the summary.
 *
 *   pnpm lint:selftest            run
 *   pnpm lint:selftest --dump     print each fixture's actual errors
 */
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FIXTURES = path.join(ROOT, "lint-fixtures");
const TOP_LEVEL_AREAS = new Set(["tests", "scripts"]);
const dump = process.argv.includes("--dump");

interface Expectation {
  rule: string;
  substring: string;
}

function parseHeader(source: string): { expectations: Expectation[] | null; virtualPath: string | null; error?: string } {
  const expectations: Expectation[] = [];
  let none = false;
  let virtualPath: string | null = null;
  for (const line of source.split("\n")) {
    if (!line.startsWith("//")) break;
    const expect = /^\/\/ expect: (.+)$/.exec(line);
    const pathLine = /^\/\/ path: (\S+)$/.exec(line);
    if (expect) {
      const body = (expect[1] as string).trim();
      if (body === "none") {
        none = true;
        continue;
      }
      const m = /^(\S+) :: (.+)$/.exec(body);
      if (!m) return { expectations: null, virtualPath, error: `malformed expectation "${body}" (want "<rule> :: <substring>")` };
      expectations.push({ rule: m[1] as string, substring: (m[2] as string).trim() });
    } else if (pathLine) {
      virtualPath = pathLine[1] as string;
    }
  }
  if (none && expectations.length) return { expectations: null, virtualPath, error: `"expect: none" mixed with expectations` };
  if (!none && !expectations.length) return { expectations: null, virtualPath, error: `missing "// expect:" header` };
  return { expectations, virtualPath };
}

const eslint = new ESLint({ cwd: ROOT });
let total = 0;
let passed = 0;

for (const area of (await readdir(FIXTURES)).sort()) {
  for (const name of (await readdir(path.join(FIXTURES, area))).sort()) {
    total++;
    const label = `${area}/${name}`;
    const source = await readFile(path.join(FIXTURES, area, name), "utf8");
    const { expectations, virtualPath, error } = parseHeader(source);
    const filePath = path.join(
      ROOT,
      virtualPath ?? (TOP_LEVEL_AREAS.has(area) ? path.join(area, "__lint_selftest__", name) : path.join("src", area, "__lint_selftest__", name)),
    );
    if (await eslint.isPathIgnored(filePath)) {
      console.log(`FAIL ${label}: virtual path ${path.relative(ROOT, filePath)} is ignored by the config`);
      continue;
    }
    const [result] = await eslint.lintText(source, { filePath });
    const errors = (result?.messages ?? []).filter((m) => m.severity === 2);

    if (dump) {
      console.log(`${label} -> ${path.relative(ROOT, filePath)}`);
      for (const m of errors) console.log(`  ${m.ruleId} :: ${m.message}`);
    }
    if (!expectations) {
      console.log(`FAIL ${label}: ${error}`);
      continue;
    }

    const unmet = expectations.filter(
      (e) => !errors.some((m) => m.ruleId === e.rule && m.message.includes(e.substring)),
    );
    const unaccounted = errors.filter(
      (m) => !expectations.some((e) => m.ruleId === e.rule && m.message.includes(e.substring)),
    );
    if (unmet.length || unaccounted.length) {
      console.log(`FAIL ${label}`);
      for (const e of unmet) console.log(`       expected, not raised: ${e.rule} :: ${e.substring}`);
      for (const m of unaccounted) console.log(`       raised, not expected: ${m.line}:${m.column} ${m.ruleId} :: ${m.message}`);
      continue;
    }
    passed++;
    if (!dump) {
      console.log(`ok   ${label}: ${expectations.length ? expectations.map((e) => e.rule).join(", ") : "clean, as expected"}`);
    }
  }
}

console.log(`\nlint self-test: ${passed}/${total} fixtures behaved as expected`);
if (passed !== total || total === 0) process.exit(1);
