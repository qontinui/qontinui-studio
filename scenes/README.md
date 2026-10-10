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
  trace/          versioned trace schema + validator; from-build-record replay adapter (stub)
  timeline.ts     trace -> beats on a frame timeline
  motion.ts       easings, interpolate, progress: pure functions of numbers
  tokens.ts       vendored design tokens (see "Design tokens")
  engine.ts       EngineAdapter / PlayerEmbedProps — the engine seam (D7)
  scene-root.tsx  what every adapter renders: canvas + scene + mode label
src/primitives/   node, edge, PR card, session lane, gate, timeline, counter, caption, mode label
src/scenes/       scenes; S1 "Plan lifecycle" is the first
src/adapters/     engines. remotion/ is the first adapter; fonts.ts is engine-neutral
src/cli/          the studio-scenes CLI
traces/           illustrative traces (fixtures)
tests/unit/       schema, motion, timeline and S1-model tests (vitest)
tests/golden/     golden-frame + determinism tests and the committed goldens
fontconfig/       the fontconfig the headless browser runs under
assets/fonts/     bundled Inter (OFL)
```

## The engine seam (plan D7)

Scenes depend on a small API that the studio owns. The trace goes in, and a
timeline plus primitives come out. They never import an engine. Remotion is the
first adapter (`src/adapters/remotion/`). It implements `EngineAdapter`
(`render`, `renderStills`, `close`) and provides a `StudioPlayer` web embed. A
different engine, such as the HyperFrames fallback named in the plan's scout
addendum, would be a second implementation of the same interfaces with no
changes to any scene.

ESLint enforces the seam. `eslint.config.js` applies `no-restricted-imports` to
`src/scenes/**` and `src/primitives/**`. It refuses:

- `remotion` and `@remotion/*`;
- other engines (Revideo, Motion Canvas, HyperFrames);
- wall-clock animation libraries (`framer-motion`/`motion`, react-spring, gsap, anime);
- any import from `src/adapters` or `src/cli`.

The `import-lint` CI job runs this.

## Scene purity

A scene is a pure function of `(trace, frame)`. `SceneRoot` gives it the
validated trace, the frame number, the layout and the timeline. It renders from
those alone. Lint enforces this in `src/scenes/**` and `src/primitives/**`. It
forbids:

- the clock and timers: `Date`, `performance`, `setTimeout`/`setInterval`,
  `requestAnimationFrame`;
- randomness: `Math.random`, `crypto`;
- `fetch`;
- React state, refs and effects;
- CSS `transition`/`animation` style properties and SVG SMIL elements.

Motion comes from `src/api/motion.ts` (`interpolate`, `progress`, `easings`,
`durations`), and its inputs are frame numbers.

Plan D2 requires every output to show whether it is illustrative or a replay. A
replay also shows its date range and source. `SceneRoot` draws the mode label
on top of the scene on every frame, using the trace's own metadata. A scene
cannot omit or restyle the label.

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
  claims a hand-authored one.

Each scene declares what it reads (`requires.kinds`, `requires.entities`). The
CLI and the adapter refuse a trace that cannot drive the scene.

Replays come from `fromBuildRecord` (`src/api/trace/from-build-record.ts`). It
is the only code that will know the shape of the factory's build-record export.
Until the sibling plan ships that export (Phase 3 here), it throws
`not implemented: needs the sibling plan's build-record export`. The export is
never reshaped to suit a scene (plan D6).

## Rendering

```sh
pnpm install
pnpm browser:ensure        # downloads the pinned Chrome Headless Shell once

pnpm studio-scenes render s1-plan-lifecycle \
  --trace traces/illustrative/s1-plan-lifecycle.json \
  --format mp4 --aspect 16:9 --out out/s1-16x9.mp4
# --format mp4|webm|gif|png   --aspect 16:9|9:16|1:1   [--scale <s>]
```

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
  the web.
- **Font configuration:** the browser runs with
  `FONTCONFIG_FILE=fontconfig/fonts.conf`. That file loads no system fonts, uses
  grayscale antialiasing, and turns off hinting and subpixel rendering. Without
  it, text rasterised differently on a Debian host and in an Ubuntu 24.04
  container (0.4–0.7 % of pixels differed). With it, the goldens are
  byte-identical on both.
- **Pure scenes:** scenes are pure, as described under "Scene purity".

## Tests

```sh
pnpm typecheck
pnpm lint            # includes the engine-import and purity rules
pnpm test            # unit: schema, motion, timeline, S1 model
pnpm test:golden     # golden frames + determinism (needs browser:ensure)
```

The golden suite renders seven S1 frames at half scale: five in 16:9 and one
each in 9:16 and 1:1. It compares them against `tests/golden/__goldens__/` using
pixelmatch, with a per-pixel threshold of 0.02, anti-aliased pixels counted,
and at most 0.01 % of pixels allowed to differ. Any change to a colour, the layout or a label fails it. The
suite also renders the same frames a second time with a fresh bundle and a
fresh browser, and requires the two renders to be byte-identical. When it
fails, the actual and diff images are in `.golden-out/`, and CI uploads them as
an artifact.

**Updating goldens** after an intended visual change:

```sh
pnpm goldens:update   # re-renders tests/golden/__goldens__/
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

- **This package:** MIT (`LICENSE`).
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
  The latin subset is from `@fontsource/inter@5.3.0`.
