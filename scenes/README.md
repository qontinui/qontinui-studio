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
src/adapters/     engines. remotion/ is the first adapter; fonts.ts and output.ts are engine-neutral
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
(`render`, `renderStills`, `close`) and provides a `StudioPlayer` web embed. A
different engine, such as the HyperFrames fallback named in the plan's scout
addendum, would be a second implementation of the same interfaces with no
changes to any scene.

ESLint enforces the seam across all of `src/**` except `src/adapters/**`.
Outside the adapters, an engine (`remotion`, `@remotion/*`, Revideo, Motion
Canvas, HyperFrames) cannot be loaded by a static import, an `export … from`,
a dynamic `import()` or a `require()`, and `import()` needs a literal
specifier. Nothing outside the adapters imports an adapter statically: the CLI
reaches the Remotion adapter only through its dynamic
`import("../adapters/remotion/render")`. Scenes and primitives additionally
may not import wall-clock animation libraries (`framer-motion`/`motion`,
react-spring, gsap, anime) or the CLI.

**Lint self-test.** `pnpm lint:selftest` (`scripts/lint-selftest.ts`) lints
every file in `lint-fixtures/<area>/` as if it lived in `src/<area>/`. Each
starts with `// expect: <rule>[, <rule>]` or `// expect: none`, and the test
fails unless every expected rule fires, the clean controls stay clean (an
engine import inside `src/adapters`, the CLI's dynamic adapter import, a pure
scene), and no virtual path is ignored by the config. CI therefore proves the
rules refuse, not only that today's code has no violation. The `import-lint`
CI job runs both `pnpm lint` and the self-test.

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
- module-level `let`/`var` (state that survives between frames);
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
title-safe width rather than clipping; `source.ref` is capped at 120
characters so it needs at most three lines at 9:16. Both mode badges use
neutral token colours (border/brand), not attention-layer state colours.

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
  `scripts/gen-glyph-coverage.py`.

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
(`src/api/trace/testing.ts`), which refuses to run outside vitest and is not
reachable from the CLI or the player.

## Rendering

```sh
pnpm install
pnpm browser:ensure        # downloads the pinned Chrome Headless Shell once

pnpm studio-scenes render s1-plan-lifecycle \
  --trace traces/illustrative/s1-plan-lifecycle.json \
  --format mp4 --aspect 16:9 --out out/s1-16x9.mp4
# --format mp4|webm|gif|png   --aspect 16:9|9:16|1:1   [--frame <n>] [--scale <s>] [--force]
```

- `--trace <file>` takes an illustrative trace; `--build-record <export>` is
  the only replay path (refused until Phase 3, see "Traces").
- `--scale` must give even whole pixel sizes at every aspect (0.25, 0.3, 0.5,
  1, 2 …); others are refused. `--frame` is checked against the scene's
  duration before anything is bundled.
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

The same trace always produces the same frames:

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
- **Pure scenes:** scenes are pure, as described under "Scene purity".

## Tests

```sh
pnpm typecheck
pnpm lint            # includes the engine-seam and purity rules
pnpm lint:selftest   # proves those rules refuse (lint-fixtures/)
pnpm test            # unit: schema, motion, timeline, S1 model, safety, CLI refusals
pnpm test:golden     # golden frames + determinism (needs browser:ensure)
```

The golden suite renders ten S1 frames at half scale: five in 16:9, one each in
9:16 and 1:1, and one per aspect of a replay with the longest allowed source
(the mode label must wrap, not clip). Each must match its golden in
`tests/golden/__goldens__/` **exactly**: zero differing pixels at pixelmatch
threshold 0 with anti-aliased pixels counted. pixelmatch only counts and draws
the diff image. Any visual change fails it, down to one digit of a PR number.
The suite also renders the same frames a second time with a fresh bundle and a
fresh browser, and requires the two renders to be byte-identical. When it
fails, the actual and diff images are in `.golden-out/`, and CI uploads them as
an artifact.

**Updating goldens** after an intended visual change:

```sh
pnpm goldens:update   # the golden suite with UPDATE_GOLDENS=1: re-renders and replaces the PNGs
# inspect the PNGs, then commit them with the change
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
- **Inter** (`assets/fonts/`): SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
  Inter 4.1, full character set (latin, latin-ext, Greek, Cyrillic, Vietnamese,
  arrows and symbols; no CJK), from `inter-ui@4.1.1` (`web/Inter-*.woff2`).
