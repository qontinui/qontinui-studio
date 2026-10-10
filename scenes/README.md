# @qontinui/studio-scenes

The scene framework of the Qontinui studio: animated explainers of how the
software factory works, drawn by code from an **event trace**. One scene renders
both as a video file (mp4, webm, gif, png) and as an embeddable web player.

Plan: `2026-10-09-qontinui-marketing-studio-animated-process-explainers`
(qontinui-dev-notes), Phase 2. This package is the tool. Traces, renders and
assets made for a particular production live in that production's own repository
(plan D6a). The one trace committed here is the illustrative fixture the tests use.

## Layout

```text
src/api/          the studio scene API — the only thing scenes import (besides primitives)
  trace/          versioned trace schema + validator; replay provenance; from-build-record adapter (stub)
  text-coverage.ts, glyph-coverage.ts   which characters the bundled fonts can draw
  timeline.ts     trace -> beats on a frame timeline
  motion.ts       easings, interpolate, progress: pure functions of numbers
  tokens.ts       vendored design tokens (see "Design tokens")
  engine.ts       EngineAdapter / PlayerEmbedProps — the engine seam (D7)
  scene-root.tsx  what every adapter renders: canvas + scene + mode label
src/primitives/   node, edge, PR card, session lane, gate, timeline, counter, caption, mode label
src/scenes/       scenes; S1 "Plan lifecycle" is the first
src/adapters/     engines. remotion/ is the first adapter, hyperframes/ the tested fallback;
                  fonts.ts and output.ts are engine-neutral
docker/hyperframes/  the pinned HyperFrames render image (Dockerfile + npm lockfile)
docs/             hyperframes-spike.md: the HyperFrames adapter spike result
src/cli/          the studio-scenes CLI
traces/           illustrative traces (fixtures)
tests/unit/       schema, motion, timeline, S1 model, safety and CLI-refusal tests (vitest)
tests/golden/     golden-frame + determinism tests and the committed goldens
lint-fixtures/    files that MUST fail lint (the lint self-test); never part of the build
fontconfig/       the fontconfig the headless browser runs under
assets/fonts/     bundled Inter 4.1, full character set (OFL)
```

## The engine seam (plan D7)

Scenes depend on a small API that the studio owns. The trace goes in, and a
timeline plus primitives come out. They never import an engine. Remotion is the
first adapter (`src/adapters/remotion/`). It implements `EngineAdapter`
(`render`, `renderStills`, `close`) and provides a `StudioPlayer` web embed. HyperFrames
(`src/adapters/hyperframes/`, Apache-2.0) is the second implementation of the
same interfaces, with no changes to any scene: `studio-scenes render …
--engine hyperframes` renders in its pinned Docker image (`pnpm
hyperframes:image`). The spike that confirmed it as the fallback, with its
determinism, parity and timing measurements, is `docs/hyperframes-spike.md`.

Two mechanisms enforce the seam.

- **The resolved module graph is the authority.** `pnpm test:graph`
  (`tests/unit/engine-graph.test.ts`, also part of `pnpm test`) bundles every
  `.ts`/`.tsx` file under `src/` except `src/adapters/**` and `src/cli/**`, one
  entry point each, with esbuild, externalising only Node builtins, and fails
  if any module in a graph is under
  `node_modules/remotion/`, `node_modules/@remotion/`, a HyperFrames package
(`hyperframes`, `@hyperframes/*`, `@heygen/*`) or `src/adapters/`. That
  catches indirection lint cannot see, such as a re-export through a module
  outside `src/`. A control case checks that the adapter's own graph does reach
  Remotion, so the test cannot pass vacuously.
- **ESLint** gives the early, per-file error. Everywhere in `src/**` except
  `src/adapters/**`, an engine (`remotion`, `@remotion/*`, Revideo, Motion
  Canvas, HyperFrames) cannot be loaded by a static import, `export … from`,
  `import()` or `require()`; `module`/`node:module` and `createRequire` are
  refused, as are `process.getBuiltinModule` and any computed access on
  `process`; and `import()` needs a literal specifier. No file outside the
  adapters imports an adapter statically, and only `src/cli/**` may load one
  with `import()` (it does, `import("../adapters/remotion/render")`). Scenes
  and primitives additionally may not import wall-clock animation libraries
  (`framer-motion`/`motion`, react-spring, gsap, anime), `react-dom` or the
  CLI.

**Lint self-test.** `pnpm lint:selftest` (`scripts/lint-selftest.ts`) lints
every file in `lint-fixtures/<area>/` as if it lived at
`src/<area>/__lint_selftest__/` (or `tests/…`, `scripts/…`, or an explicit
`// path:`). Each fixture declares `// expect: <rule-id> :: <message
substring>` lines or `// expect: none`. A fixture passes only when every
expectation is met by an error with that rule id and message, and every error
it raised is expected. A missing header or an ignored virtual path fails, and
every fixture is counted in the summary. The `import-lint` CI job runs `pnpm
lint`, the self-test and the graph test.

## Scene purity

A scene is a pure function of `(trace, frame)`. `SceneRoot` gives it the
validated trace, the frame number, the layout and the timeline. It renders from
those alone. Lint enforces this in `src/scenes/**` and `src/primitives/**`. It
forbids:

- ambient globals that reach the clock, randomness or the DOM: `globalThis`,
  `window`, `self`, `global`, `document`, `navigator`, `process`, `Intl`;
- the clock and timers: `Date`, `performance`, `process.hrtime`,
  `setTimeout`/`setInterval`, `queueMicrotask`, `requestAnimationFrame`;
- randomness: `Math.random`, `crypto`; `Math` may be used only as
  `Math.<name>` (no aliasing, no computed access);
- `fetch`;
- React state, refs and effects, by named import or through a default or
  namespace `React` import;
- module-level `let`/`var`, and module-level `new Map/Set/WeakMap/WeakSet/WeakRef/Array`
  (state that survives between frames). Module-level array and object
  *literals* are allowed, because scenes use them as constant lookup tables
  (`STAGES`, captions). Lint cannot see a later mutation of one; the
  determinism test is the backstop for that;
- `eval`, string `setTimeout`/`setInterval` and `new Function` (everywhere,
  not only in scenes), and any `.constructor` access in scenes (the
  `x.constructor.constructor` route to `Function`);
- blur of any kind (see "Determinism"): `box-shadow` other than
  `solidRings(...)`, `filter`, `backdrop-filter`, `text-shadow`, `blur()` or
  `drop-shadow()` in strings, SVG filter elements;
- `react-dom` (e.g. `createPortal`, which would escape the scene and its mode
  label);
- media that plays on its own clock: `<video>`, `<audio>`, `<iframe>`,
  `<marquee>`, any `autoPlay` attribute, and an `<img>` whose literal `src` is
  a `.gif`/`.apng`;
- CSS-timed motion in any spelling: `transition`/`animation` keys, prefixed
  (`WebkitAnimation`) or string (`"-webkit-transition"`) keys, computed keys,
  `@keyframes`/`transition:` in strings, `<style>` elements,
  `dangerouslySetInnerHTML` and SVG SMIL elements.

Motion comes from `src/api/motion.ts` (`interpolate`, `progress`, `easings`,
`durations`), and its inputs are frame numbers.

Plan D2 requires every output to show whether it is illustrative or a replay. A
replay also shows its date range and source. `SceneRoot` draws the mode label
on every frame from the trace's own metadata, in a layer above the scene; the
scene sits in its own stacking context (`isolation: isolate`), so it cannot
omit, restyle or cover the label. The label's detail line wraps within the
title-safe width and is clamped to three lines with an ellipsis, and every
scene layout starts its header below that room, so the label can neither clip
sideways nor reach the header. The full source always stays in the trace;
`source.ref` is capped at 120 characters. S1's plan title is clamped to two
lines. Goldens cover a 120-character realistic source, a source of 120 "W"s
and a 160-character title. Both mode badges use neutral token colours
(border/brand), not attention-layer state colours.

## Traces

`src/api/trace/schema.ts` is the schema. It uses zod, it is strict, and it
carries `trace_version: 1`. A trace contains:

- `meta`:
  - `mode` (`illustrative` | `replay`) and `title`;
  - `source`: `hand-authored` for illustrative traces; `build-record` with an
    `export_version` for replays;
  - `date_range`: required for a replay.
- `entities`: `plans`, `prs` and `sessions`.
- `events`: each event has an `id` and an `at` (ISO-8601 with an offset), and is
  one of:
  - `plan.drafted|vetted|implementing|shipped`
  - `session.started|ended`
  - `pr.opened`
  - `ci.started|passed|failed`
  - `merge_train.queued`
  - `pr.landed`

The validator also checks that:

- every id is unique and every reference resolves;
- replay events fall inside `date_range`;
- an illustrative trace never claims a build-record source, and a replay never
  claims a hand-authored one;
- every character of every string can be drawn by the bundled fonts. A
  character outside that set (e.g. CJK) is refused, named with its code point,
  rather than rendered as a missing-glyph box or a machine-dependent fallback.
  `src/api/glyph-coverage.ts` is generated from the fonts by
  `scripts/gen-glyph-coverage.py` and records each font file's SHA-256; a unit
  test fails if a font changes without the table being regenerated.

Each scene declares what it reads (`requires.kinds`, `requires.entities`) and
may add shape checks (`check`; S1 needs exactly one plan and at most one PR).
The CLI, the adapter and the player refuse a trace that cannot drive the scene.

**Replays are refused unless they come from a build record (plan D2).** The
schema accepts the replay shape, but anyone can hand-write `"mode": "replay"`,
so shape is not provenance. A replay renders only when the trace object was
built in-process by `fromBuildRecord` (`src/api/trace/from-build-record.ts`),
which records it in `src/api/trace/provenance.ts`; the CLI (`--build-record
<export>`), every adapter entry point and the web player check that record. A
copy of the object, or any trace read from JSON, does not carry it. Until the
sibling plan ships the export (Phase 3 here), `fromBuildRecord` throws
`not implemented: needs the sibling plan's build-record export`, so every
replay is refused. The export is never reshaped to suit a scene (plan D6).
Tests that must draw the REPLAY label use `attestReplayForTests`
(`src/api/trace/testing.ts`), which refuses to run outside vitest. Lint keeps
both doors narrow: `recordReplayProvenance` may appear only in
`provenance.ts`, `from-build-record.ts` and `testing.ts` (by name, as a string
key, through `export * from` the provenance module, through a namespace import
of it, or through `import()` of it), and `trace/testing` may be imported only
under `tests/**`.

**Phase 3 caveat.** The attestation lives on the object `fromBuildRecord`
returned. Anything that re-parses or copies it (`parseTrace`, `validateTrace`,
`structuredClone`, a JSON round trip, `postMessage`) produces an unattested
trace, which every render path refuses. A replay must therefore be built in
the process that renders it and passed through unchanged; the web player must
receive the object `fromBuildRecord` returned, not a serialised copy.

## Rendering

```sh
pnpm install
pnpm browser:ensure        # downloads the pinned Chrome Headless Shell once

pnpm studio-scenes render s1-plan-lifecycle \
  --trace traces/illustrative/s1-plan-lifecycle.json \
  --format mp4 --aspect 16:9 --out out/s1-16x9.mp4
# --format mp4|webm|gif|png   --aspect 16:9|9:16|1:1   [--frame <n>] [--force]
# [--scale <s> --allow-nondeterministic]
```

- `--trace <file>` takes an illustrative trace; `--build-record <export>` is
  the only replay path (refused until Phase 3, see "Traces").
- **Only scale 1 is reproducible, so any other `--scale` is refused** unless
  `--allow-nondeterministic` is given. Measured after the blur fix: at
  `--scale 0.5`, one edge pixel of the S1 gate differed by 1 in one channel in
  3 of 60 frames over 6 repeated renders. A permitted scale must still give
  even whole pixel sizes at every aspect (0.25, 0.3, 0.5, 2 …). `--frame` is
  checked against the scene's duration before anything is bundled.
- **Output safety.** A render writes into a fresh staging directory beside the
  target and then moves the result into place. It never deletes anything it
  did not write, and never recursively. An existing output file is refused
  unless `--force` is given (which replaces just that file); a directory in its
  place is always refused. A png sequence target must be absent, empty, or
  hold only `element-*.png` from a previous run; `--force` replaces exactly
  those files, and any other file in the directory makes the render refuse.

`bin/studio-scenes.mjs` exposes the same commands as `studio-scenes`. The other
commands are `studio-scenes validate <trace> [--scene <id>]` and
`studio-scenes list`.

The output sizes are 1920x1080, 1080x1920 and 1080x1080, all at 30 fps.

- **png:** with `--frame <n>`, the CLI renders one still to the `--out` file.
  Without it, it renders every frame as a PNG sequence into the `--out`
  directory (`element-000.png`, …).
- **mp4:** H.264.
- **webm:** VP8.
- **gif:** every second frame.

S1 with the illustrative trace runs 14.2 s (426 frames).

### Determinism

What the tests prove (16:9 and the golden frames, at scale 1, on the machines
named below): the same trace gives byte-identical frames across fresh
browsers, regardless of which frames were rendered before, and through the
CLI's parallel `renderFrames`/`renderMedia` path (two png-sequence runs and two
mp4 runs, compared by SHA-256). What it rests on:

- **Own server port:** each adapter gives Remotion's local file server a port
  the OS reports free, so concurrent renders do not contend for Remotion's
  default port. If a page still gets no response (the port was taken in
  between), that one call is retried once on a fresh port; nothing else is
  retried.
- **Pinned browser:** the browser is the Chrome Headless Shell build pinned by
  the exact `@remotion/renderer` version. Every dependency version is exact,
  and so is the lockfile.
- **Software rendering:** rendering uses software GL (`swangle`), at a fixed
  frame rate, with frame-seeked capture.
- **Fonts:** fonts are bundled and loaded by the page; nothing is fetched from
  the web, and trace text the fonts cannot draw is refused at validation.
- **Font configuration:** the browser is launched with
  `FONTCONFIG_FILE=fontconfig/fonts.conf`. That file loads no system fonts, uses
  grayscale antialiasing, and turns off hinting and subpixel rendering.
  Remotion's `openBrowser` takes no environment option, so the adapter sets the
  variable only around the launch and restores the previous value in `finally`.
  Without the file, text rasterised differently on the Debian development host
  and in a local `ubuntu:24.04` container (0.4–0.7 % of pixels). With it, the
  goldens were byte-identical on those two. That is two machines, not every
  machine: CI (`ubuntu-24.04`) is the authority.
- **No blur:** Chrome caches blurred masks across paints. A `box-shadow` glow
  with a blur radius made frame 196 of S1 hash one way when rendered alone or
  after frame 0, and another way after frame 48 (9,179 px, up to 4 levels, all
  inside the glow), and made frames differ between two CLI sequence runs (7 of
  426 in the review's run, 47 of 426 when the CLI test was run against the old
  glow). Removing only the blur made frame 196 identical in every order. Glows
  are now solid zero-blur rings (`solidRings`/`glowRings` in `src/api/glow.ts`),
  and lint refuses blur in scenes and primitives: `box-shadow` other than
  `solidRings(...)` (and `solidRings` must be the one imported from
  `src/api`, never a local binding of that name), `filter`,
  `backdrop-filter`, `text-shadow`, `blur()`/`drop-shadow()` in strings, and
  SVG filter elements. Lint cannot see a style object assembled at run time
  (for example with `Object.fromEntries`); that escape is left to the
  order-independence tests, which would catch any blur it smuggled in.
- **Pure scenes:** scenes are pure, as described under "Scene purity".

## Tests

```sh
pnpm typecheck
pnpm lint            # includes the engine-seam and purity rules
pnpm lint:selftest   # proves those rules refuse (lint-fixtures/)
pnpm test:graph      # the engine seam on the resolved module graph
pnpm test            # unit: schema, motion, timeline, S1 model, safety, CLI refusals
pnpm test:golden     # golden frames, determinism, order independence, CLI path (needs browser:ensure)
pnpm test:hyperframes  # opt-in (HYPERFRAMES=1, needs `pnpm hyperframes:image`): HyperFrames determinism
```

The golden suite renders fourteen S1 frames: five in 16:9, one each in 9:16
and 1:1, one per aspect of a replay with the longest realistic source, and at
9:16 and 1:1 a replay whose source is 120 "W"s and a trace with a
160-character title, all at native size (scale 1).

- **Golden gate (exact):** each full-size frame's SHA-256 must equal the
  committed `__goldens__/<name>.png.sha256`. Beside it is a 2x2-reduced PNG
  (deterministic box filter in JavaScript) for humans; on a mismatch the
  reduced frames are diffed with pixelmatch only to draw the diff image. Any
  visual change fails, down to one digit of a PR number.
- **Determinism:** every frame is rendered again with a fresh browser and must
  be byte-identical.
- **Order independence:** every frame is rendered again in reversed order, and
  the five 16:9 frames plus one 9:16 and one 1:1 frame are each rendered alone
  in a fresh browser; all must be byte-identical to the suite-order render.
- **CLI path** (`tests/golden/cli-determinism.test.ts`, 16:9 only to bound CI
  time): two full `render --format png` sequence runs (426 frames) must match
  frame by frame, and two `render --format mp4` runs must have the same
  SHA-256.

When it fails, the actual and diff images are in `.golden-out/`, and CI uploads
them as an artifact (without the CLI test's frame sequences).

**Updating goldens** after an intended visual change:

```sh
pnpm goldens:update   # golden.test.ts with UPDATE_GOLDENS=1: rewrites the reduced PNGs and .sha256 files
# inspect the PNGs, then commit them (and the .sha256 files) with the change
```

## Design tokens

Plan D4 makes `@qontinui/design-tokens` the palette authority. That package's
repository is now **AGPL-3.0-or-later**, and its only npm release (1.0.0, MIT)
predates the attention palette. This MIT package therefore cannot depend on it.
Instead, `src/api/tokens.ts` **vendors** the values verbatim from
`qontinui-design-tokens/src/colors.ts` at 82873452 (version 1.2.0), with line
citations.

State colours (PR card, gate, session lane, stage nodes) come only from the
attention layer:

| Level | Meaning |
| --- | --- |
| attention | someone must act |
| waiting | waiting on something else |
| running | work in flight |
| testing | rebasing, re-running or verifying |
| landing | on its way to done |
| done | finished |
| inert | nothing is happening |

Scenes mint no colour.

Motion durations and easings (`src/api/motion.ts`) and the type scale
(`src/api/typography.ts`) are studio-local for now. **Phase 1 moves the motion
and typography tokens into `@qontinui/design-tokens`.** These files then become
imports, or a vendored copy if the licences still differ.

## Licences

- **This package:** the qontinui-studio repository is MIT, which covers
  `scenes/` too (see the root `LICENSE` once studio#1 lands). `scenes/LICENSE`
  and `"license": "MIT"` in `package.json` state the same terms for this
  package.
- **Remotion** (`remotion`, `@remotion/*`): source-available, not OSI open
  source. It is declared as an exact-version `peerDependency`, plus a
  `devDependency` so the package can run. It is never vendored, and only
  `src/adapters/remotion/` imports it. The plan's Phase 0 confirmed that
  Qontinui qualifies for Remotion's free licence: an individual, or an
  organisation of up to 3 people with contractors counted. Each of the
  following ends that eligibility:
  - the headcount reaches 4;
  - tenants upload their own scene code;
  - qontinui-web cannot adopt its AGPL §7 additional permission.

  The adapter passes **no `licenseKey`**, so Remotion sends no usage event. No
  cloud or Lambda rendering is used. Re-read the terms before upgrading to
  Remotion 5.0.
- **AGPL consumers:** qontinui-web may embed `StudioPlayer` only under the §7
  additional permission that names Remotion (plan Phase 4). It loads the player
  as a separate lazy chunk and states Remotion's licence in its NOTICE.
- **HyperFrames** (`hyperframes`, `@hyperframes/*`): Apache-2.0, no
  additional permission needed anywhere. Exact-version `devDependencies`
  (types and the embed check); the CLI runs only inside
  `docker/hyperframes/`'s image, with `HYPERFRAMES_NO_TELEMETRY=1`,
  `DO_NOT_TRACK=1` and `--network none` (its CLI sends PostHog telemetry by
  default).
- **Inter** (`assets/fonts/`): SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
  Inter 4.1, full character set (latin, latin-ext, Greek, Cyrillic, Vietnamese,
  arrows and symbols; no CJK), from `inter-ui@4.1.1` (`web/Inter-*.woff2`,
  "Version 4.001;git-9221beed3"). The copyright line in `OFL.txt` is the one in
  the fonts' own name table ("Copyright 2016 The Inter Project Authors").
