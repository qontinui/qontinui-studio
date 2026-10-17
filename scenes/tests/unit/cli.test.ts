/**
 * CLI refusals. Every case here is refused BEFORE bundling or launching a
 * browser, so these run in the unit suite.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { clone, replayTraceJson, s1TraceJson } from "./fixtures";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const BIN = path.join(ROOT, "bin/studio-scenes.mjs");
const S1_TRACE = path.join(ROOT, "traces/illustrative/s1-plan-lifecycle.json");

function cli(...args: string[]) {
  const r = spawnSync(process.execPath, [BIN, ...args], { cwd: ROOT, encoding: "utf8", timeout: 60_000 });
  return { status: r.status, stderr: r.stderr, stdout: r.stdout };
}

function tmpFile(name: string, content: unknown): string {
  const dir = mkdtempSync(path.join(tmpdir(), "studio-cli-"));
  const file = path.join(dir, name);
  writeFileSync(file, JSON.stringify(content));
  return file;
}

const base = ["render", "s1-plan-lifecycle", "--format", "mp4", "--aspect", "16:9"];

describe("studio-scenes CLI refusals", () => {
  it("refuses a hand-written replay trace (plan D2)", () => {
    const r = cli(...base, "--trace", tmpFile("replay.json", replayTraceJson()), "--out", "/tmp/never.mp4");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/refusing to render a replay trace that did not come from a build-record export/);
  });

  it("--build-record is the replay path, and is not available before Phase 3", () => {
    const r = cli(...base, "--build-record", tmpFile("export.json", { export_version: "1" }), "--out", "/tmp/never.mp4");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/not implemented: needs the sibling plan's build-record export/);
  });

  it("needs exactly one of --trace and --build-record", () => {
    expect(cli(...base, "--out", "/tmp/never.mp4").stderr).toMatch(/exactly one of --trace or --build-record/);
  });

  it("turns parseArgs errors into usage errors", () => {
    const r = cli(...base, "--trace", S1_TRACE, "--out", "/tmp/x.mp4", "--bogus");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/Unknown option '--bogus'/);
    expect(r.stderr).toMatch(/^usage:/m);
  });

  it("checks --frame against the scene duration before rendering", () => {
    const r = cli("render", "s1-plan-lifecycle", "--trace", S1_TRACE, "--format", "png", "--aspect", "16:9", "--frame", "426", "--out", "/tmp/x.png");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/--frame 426 is outside 0\.\.425/);
  });

  it("refuses any scale but 1 unless --allow-nondeterministic", () => {
    const r = cli(...base, "--trace", S1_TRACE, "--scale", "0.5", "--out", "/tmp/x.mp4");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/--scale 0\.5 is refused: renders are byte-reproducible only at scale 1/);
  });

  it("refuses an unknown --engine", () => {
    const r = cli(...base, "--trace", S1_TRACE, "--engine", "motion-canvas", "--out", "/tmp/x.mp4");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/--engine must be one of remotion, hyperframes/);
  });

  it("refuses any scale but 1 with --engine hyperframes, even with --allow-nondeterministic", () => {
    const r = cli(...base, "--trace", S1_TRACE, "--engine", "hyperframes", "--scale", "0.5", "--allow-nondeterministic", "--out", "/tmp/x.mp4");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/--engine hyperframes renders at scale 1 only/);
  });

  it("refuses a scale that gives odd or fractional dimensions", () => {
    const r = cli(...base, "--trace", S1_TRACE, "--scale", "0.33", "--allow-nondeterministic", "--out", "/tmp/x.mp4");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/even whole pixels/);
  });

  it("refuses a trace with two plans for S1", () => {
    const json = clone(s1TraceJson());
    json.entities!.plans!.push({ id: "plan-2", title: "Another" });
    const r = cli(...base, "--trace", tmpFile("two.json", json), "--out", "/tmp/x.mp4");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/exactly one plan \(the trace has 2\)/);
  });

  it("refuses a png sequence into a directory holding other files, and deletes nothing", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "studio-cli-out-"));
    writeFileSync(path.join(dir, "notes.txt"), "mine");
    mkdirSync(path.join(dir, "sub"));
    writeFileSync(path.join(dir, "sub", "keep.md"), "mine");
    const r = cli("render", "s1-plan-lifecycle", "--trace", S1_TRACE, "--format", "png", "--aspect", "16:9", "--out", dir, "--force");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/holds files this tool did not write \(notes\.txt, sub\)/);
    expect(readdirSync(dir).sort()).toEqual(["notes.txt", "sub"]);
    expect(readFileSync(path.join(dir, "sub", "keep.md"), "utf8")).toBe("mine");
  });

  it("refuses to overwrite an existing output file without --force", () => {
    const file = path.join(mkdtempSync(path.join(tmpdir(), "studio-cli-out-")), "s1.mp4");
    writeFileSync(file, "existing");
    const r = cli(...base, "--trace", S1_TRACE, "--out", file);
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/refusing to overwrite .*s1\.mp4: it exists \(pass --force/);
    expect(readFileSync(file, "utf8")).toBe("existing");
  });

  it("validate notes that a replay is valid in shape but not renderable", () => {
    const r = cli("validate", tmpFile("replay.json", replayTraceJson()));
    expect(r.status).toBe(0);
    expect(r.stdout).toMatch(/NOT renderable/);
  });
});

describe("studio-scenes compose refusals", () => {
  const compose = (...args: string[]) => cli("compose", "teaser", "--format", "mp4", "--aspect", "16:9", ...args);
  const out = () => path.join(mkdtempSync(path.join(tmpdir(), "studio-compose-")), "teaser.mp4");

  it("lists the compositions", () => {
    const r = cli("compositions");
    expect(r.status).toBe(0);
    expect(r.stdout.split("\n").map((l) => l.split("\t")[0])).toEqual(expect.arrayContaining(["teaser", "explainer", "factory-pulse"]));
  });

  it("refuses an unknown composition and a format that carries no audio", () => {
    expect(cli("compose", "nope", "--format", "mp4", "--aspect", "16:9", "--out", out()).stderr).toMatch(/unknown composition "nope"/);
    expect(cli("compose", "teaser", "--format", "gif", "--aspect", "16:9", "--out", out()).stderr).toMatch(/--format must be one of mp4, webm/);
  });

  it("exits 5 when --build-record cannot build a replay, so a caller can fall back", () => {
    const r = compose("--build-record", tmpFile("export.json", { export_version: "1" }), "--out", out());
    expect(r.status).toBe(5);
    expect(r.stderr).toMatch(/not implemented: needs the sibling plan's build-record export/);
  });

  it("refuses --trace for a segment the composition does not have, and a replay trace", () => {
    expect(compose("--trace", `ghost=${S1_TRACE}`, "--out", out()).stderr).toMatch(/no scene segment "ghost"/);
    const r = compose("--trace", `lifecycle=${tmpFile("replay.json", replayTraceJson())}`, "--out", out());
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/refusing to render a replay trace/);
  });

  it("refuses b-roll without a provenance sidecar, or one that does not match the image", () => {
    const image = tmpFile("still.png", "not really a png");
    expect(compose("--broll", `teaser-closing=${image}`, "--out", out()).stderr).toMatch(/b-roll provenance/);
    writeFileSync(`${image}.provenance.json`, JSON.stringify({ ai_generated: true, provider: { id: "local" }, file: { sha256: "0".repeat(64) } }));
    expect(compose("--broll", `teaser-closing=${image}`, "--out", out()).stderr).toMatch(/file\.sha256 0+ is not the image's/);
    expect(compose("--broll", `nope=${image}`, "--out", out()).stderr).toMatch(/b-roll slot "nope" is not a slot of teaser/);
  });

  it("refuses narration voiced for another composition, or a clip that does not hash", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "studio-narration-"));
    writeFileSync(path.join(dir, "open.wav"), "audio");
    const table = (composition: string) => {
      const file = path.join(dir, `${composition}.json`);
      writeFileSync(
        file,
        JSON.stringify({
          timing_version: 1,
          composition,
          script_sha256: "0".repeat(64),
          voice: "test",
          segments: { open: { audio: "open.wav", audio_sha256: "1".repeat(64), duration_seconds: 1 } },
        }),
      );
      return file;
    };
    expect(compose("--narration", table("explainer"), "--out", out()).stderr).toMatch(/voiced for "explainer", not "teaser"/);
    expect(compose("--narration", table("teaser"), "--out", out()).stderr).toMatch(/does not hash to the table's audio_sha256/);
  });

  it("needs every card variable of the factory pulse", () => {
    const r = cli("compose", "factory-pulse", "--format", "mp4", "--aspect", "16:9", "--out", out(), "--var", "week=2026-W41");
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/variable "status_line" is not set/);
  });

  it("refuses to overwrite an existing manifest without --force", () => {
    const file = out();
    writeFileSync(`${file}.composition.json`, "{}");
    const r = compose("--out", file);
    expect(r.status).toBe(2);
    expect(r.stderr).toMatch(/refusing to overwrite .*teaser\.mp4\.composition\.json/);
  });
});
