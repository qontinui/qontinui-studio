# @qontinui/studio-scenes

The scene framework of the Qontinui studio: animated explainers of how the
software factory works, drawn by code from an **event trace**. One scene renders
both as a video file (mp4, webm, gif, png) and as an embeddable web player.

Plan: `2026-10-09-qontinui-marketing-studio-animated-process-explainers`
(qontinui-dev-notes), Phases 2 (framework) and 3 (scenes). This package is the
tool. Traces, renders and assets made for a particular production live in that
production's own repository (plan D6a). The traces committed here are the scenes' illustrative fixtures,
one per scene, which the tests and the embed page use.

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
src/primitives/   node, edge, PR card, session lane, gate, timeline, counter, caption, mode label,
                  feed (a panel of short rows), scene header, label (a small pill on a point)
src/scenes/       the scenes of the plan's catalogue (S1 "Plan lifecycle", S2 "The digital twin",
                  S3 "Gates and continuations", S4 "The merge train", S5 "The fleet",
                  S6 "Self-correction", S7 "Model-based GUI automation")
src/adapters/     engines. remotion/ is the first adapter, hyperframes/ the tested fallback;
                  fonts.ts and output.ts are engine-neutral
docker/hyperframes/  the pinned HyperFrames render image (Dockerfile + npm lockfile)
docs/             hyperframes-spike.md: the HyperFrames adapter spike result
src/cli/          the studio-scenes CLI
traces/           illustrative traces (fixtures)
tests/unit/       schema, motion, timeline, per-scene model, safety, catalogue and CLI-refusal tests (vitest)
tests/golden/     golden-frame + determinism tests and the committed goldens (every scene)
tests/embed/      the embed test page (the web player as a consumer mounts it) and its server
tests/matrix/     the scene matrix: every scene x aspect renders, embeds, shows its D2 label, honours reduced motion
lint-fixtures/    files that MUST fail lint (the lint self-test); never part of the build
fontconfig/       the fontconfig the headless browser runs under
assets/fonts/     bundled Inter 4.1, full character set (OFL)
```

## Scenes

| Scene | Shows | Trace |
| --- | --- | --- |
| `s1-plan-lifecycle` | one plan, draft to shipped | illustrative; replay once the build-record export lands in coord |
| `s2-digital-twin` | GitHub, CI, machines and sessions mirrored into coord; decisions from the twin; the developer twin answering from served documents | illustrative only: the export carries no twin, webhook or question events |
| `s3-gates-continuations` | a session registers a gate on a condition and ends; coord observes the condition and clears the gate; a new session continues | illustrative only: the export carries gate counts, not gate events |
| `s4-merge-train` | PRs queued, rebased onto main, tested and landed in order; a conflicted PR steps out and rejoins at the back | illustrative only: the export carries land times but no queue order, rebase or conflict events |
| `s5-fleet` | sessions on several machines, each in its own worktree, coordinating through declared intent and claims; a held claim is a collision avoided | illustrative only: the export carries a session count, excludes machine names, and has no worktree, intent or claim events |
| `s6-self-correction` | CI goes red; the session diagnoses it from the CI log and pushes a fix; CI runs again, round the loop to green, with no human step | illustrative only: the export carries attempts, CI duration and a red-CI-fixed count, but no CI run, diagnosis or fix events |
| `s7-gui-automation` | a state graph learned from an app (states and actions found while exploring), then traversed by actions to a goal: the original Qontinui idea, now one module of the factory | illustrative by design |

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
lines. Goldens cover a 120-character source (a labelled test fixture), a source of 120 "W"s
and a 160-character title. Both mode badges use neutral token colours
(border/brand), not attention-layer state colours.

## Embedding and reduced motion

The adapter's web player (`StudioPlayer`, `PlayerEmbedProps` in
`src/api/engine.ts`) validates the trace it is given and refuses an unproven
replay (see "Traces"). Each scene declares a **poster frame**
(`SceneDefinition.poster`, default its last frame; `posterFrame()` in
`src/api/scene-root.tsx`): the frame that tells the scene's story on its own.

- An embed that is not autoplaying opens on the poster frame.
- Under `prefers-reduced-motion: reduce` an embed **never autoplays**, whatever
  `autoPlay` says. It shows the poster frame and a play control (even when
  `controls` is false) and plays only when the viewer presses it. Until the
  preference is known (server rendering) it assumes reduced motion; a change
  of preference remounts the player.

`pnpm embed:serve` serves the embed test page (`tests/embed/page.tsx`) for a
human: `http://127.0.0.1:4173/?scene=<id>&aspect=16:9&autoplay=1`, with
`controls=0` and `ssr=1` (server-render, then hydrate). `mode=replay` (the
same events as an attested replay whose source reads "TEST FIXTURE") works
only when vitest builds the page, as the scene matrix does; under
`embed:serve` the test-only hook refuses it.

## Traces

`src/api/trace/schema.ts` is the schema. It uses zod, it is strict, and it
carries `trace_version: 1`. The version is bumped only on a breaking change;
additions (new entity lists, new event kinds) keep it, so every older v1 trace
still parses, while a trace using a newer kind is refused by an older studio
as an unknown event. A trace contains:

- `meta`:
  - `mode` (`illustrative` | `replay`) and `title`;
  - `source`: `hand-authored` for illustrative traces; `build-record` with an
    `export_version` for replays;
  - `date_range`: required for a replay.
- `entities`: `plans`, `prs`, `sessions`, `sources` (systems the twin
  mirrors: `repo-host`, `ci`, `machine`, `session`; display labels only),
  `gates` (a condition a session waits on), `machines` (display labels
  only; a session may name its `machine`), and `states` / `transitions` (a
  GUI's states and the actions between them).
- `events`: each event has an `id` and an `at` (ISO-8601 with an offset), and is
  one of:
  - `plan.drafted|vetted|implementing|shipped`
  - `session.started|ended` (`session.started` may name the gate it
    `continues`)
  - `pr.opened`
  - `ci.started|passed|failed`
  - `ci.diagnosed` and `fix.pushed`
  - `merge_train.queued|rebased|conflicted|resolved`
  - `pr.landed`
  - `twin.mirrored` (a fact from a source) and `twin.decided` (a decision and
    the twin state it was made from)
  - `question.asked` and `question.answered` (the answer, and whether it came
    from a served `policy` or `intent` document or from the `operator`; the
    answer cites the document unless the operator gave it)
  - `gate.registered` (by a session), `gate.observed` and `gate.cleared`
  - `gui.state_found`, `gui.transition_found` and `gui.transition_taken`
  - `worktree.allocated`, `intent.declared`, `claim.acquired|released` and
    `claim.contended` (naming the session that holds the claim)

The validator also checks that:

- every id is unique and every reference resolves;
- an answer references a `question.asked` event that comes strictly before
  it (by time, then by position in the trace), and each question is answered
  at most once;
- a gate is registered, observed and cleared at most once each; it is
  registered by a session that has not ended, observed and cleared only after
  it is registered, observed (when it is) before it clears, and a
  continuation starts only after its gate cleared (all by time, then by
  position);
- in the merge train, a resolution answers an unresolved conflict of the same
  PR, a conflicted PR is neither rebased nor landed until it is resolved, and
  a PR lands at most once;
- a diagnosis is made only while its PR is red, and a fix answers the latest
  red run's diagnosis, once, while the PR is still red;
- a GUI state or transition is found at most once; a transition is found
  only after both its states, taken only after it is found, and each step of
  a traversal starts where the previous one ended;
- a claim is acquired only when no session holds it (the holder may not
  re-acquire it), released only by its holder, and contended only against the
  session holding it at that point; a session's end releases its claims; and
  a session starts and ends at most once, and allocates worktrees, declares
  intent and handles claims only between its start and its end (a session
  that never starts is left to the scene's check);
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
may add shape checks (`check`; S1 needs exactly one plan and at most one PR,
S2 at most four sources, S3 at least one continuation and at most three
sessions, S4 at most four PRs, S5 at most three machines with two sessions
each, every session on a machine and started before it acts, S6 exactly
one PR, a closed loop (red, a fix, then green) and no operator step, S7 at
most six states).
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

- **Interrupts.** Ctrl-C (SIGINT), SIGTERM or a terminal hang-up (SIGHUP)
  during `render` closes the adapter. The render stops, publishes nothing, and removes its staging
  files (and, for HyperFrames, its project directory and container). The CLI
  then exits 130, 143 or 129 and says which happened: the render was stopped (or
  failed while stopping) and nothing was published, or it had already
  finished and the output is complete. A second signal, sent a second
  or more after the first, exits at once and skips that cleanup (a repeat
  within that second is taken as the same Ctrl-C relayed by a wrapper such
  as pnpm or tsx). `pnpm render` runs `bin/studio-scenes.mjs` in one node
  process.
- **Adapters are single-use.** After `close()`, every `render` or
  `renderStills` call (new or in flight) rejects with `AdapterClosed`
  (`src/api/engine.ts`), and nothing is published after `close()`.
  `close()` is idempotent. Create a new adapter to render again.
- **Remotion and SIGINT, for library callers.** Remotion's browser launcher
  installs a SIGINT listener that kills Chrome and calls `process.exit(130)`
  at once. `RemotionAdapter`'s `interrupts` option picks who handles SIGINT:
  - `"remotion"` (the default) leaves that listener alone. A program with no
    SIGINT handler of its own therefore still kills Chrome and exits 130 on
    Ctrl-C, but no staging cleanup runs (a `.studio-render-*` directory can be left
    next to the output).
  - `"caller"` (what the CLI uses) removes exactly that one listener after
    each browser launch, matched by its source
    (`killProcess(); process.exit(130);`), so listeners added by anyone else,
    even during the launch, are untouched. The caller must then handle SIGINT
    and call `close()`, which cancels the render and closes Chrome; the render
    unwinds through its staging cleanup. Remotion's `exit` listener, which
    kills Chrome on any process exit, stays in both modes.
  - `close()` never rejects. A still in flight is not cancelled but allowed
    to finish (Remotion's cancel path for `renderStill` can leave its file
    server open, keeping your process alive); its result is not published.
  - Cancelling a Remotion render leaves some of Remotion's own promises
    rejecting later with nobody awaiting them (e.g. "Target closed" from a
    page it closes without awaiting, an EPIPE from a helper process that
    already quit). By default Node would crash your process on those. So
    `close()` of an adapter with a render in flight adds, for 30 s after the
    last such close, one process `unhandledRejection` listener. It absorbs a
    rejection only when Node reports it from inside that adapter's own
    cancelled render (every render call runs in an `AsyncLocalStorage`
    context, and your `onProgress` runs outside it). For any other
    rejection it does what Node would have done without it: nothing if you
    have your own listener; otherwise it follows `--unhandled-rejections`
    from `process.execArgv` and `NODE_OPTIONS` (either spelling,
    `--unhandled-rejections=warn` or `--unhandled-rejections warn`): `throw`
    (the default) raises it as an uncaught exception; under `warn` Node
    prints its warning itself and the listener adds nothing;
    `warn-with-error-code` prints that warning once and sets exit code 1;
    `none` ignores it; under `strict` Node has already raised it.
  - The adapters' own bookkeeping of a render never counts as handling its
    rejection: a render you start and never await still surfaces as an
    unhandled rejection when it fails. Node 24 is needed for the
    attribution (it restores the rejection's async context for the
    listener); on older Node nothing is absorbed.

- **HyperFrames container deadline.** Every container renders under
  `timeout` (`docker/hyperframes/studio-hf-render.sh`) set to the adapter's
  render timeout plus 60 s (30 min + 60 s by default), so a container that
  nothing manages any more, e.g. after the CLI was killed with SIGKILL,
  still ends: SIGTERM, then SIGKILL 30 s later, then `--rm` removes it. The
  staging and project directories of such a run are left behind (nothing is
  left to remove them).
- **HyperFrames container resources.** A HyperFrames render runs with
  `--memory` 8g, `--cpus` 8, `--pids-limit` 8192 and a 4g tmpfs at `/tmp`
  (`STUDIO_HF_MEMORY`, `STUDIO_HF_CPUS`, `STUDIO_HF_PIDS_LIMIT`,
  `STUDIO_HF_TMPFS`). The tmpfs is memory, so what HyperFrames writes there
  (work files, captured frames, the finished artifact) counts against
  `--memory` together with Chrome. For long or 4K renders, raise
  `STUDIO_HF_TMPFS` (a 4K frame is about 4× a 1080p one) and raise
  `STUDIO_HF_MEMORY` by at least as much. Otherwise the container is killed
  for running out of memory before the tmpfs fills.

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
pnpm test            # unit: schema, motion, timeline, scene models, safety, catalogue, CLI refusals
pnpm test:golden     # golden frames, determinism, order independence, CLI path (needs browser:ensure)
pnpm test:hyperframes  # opt-in (HYPERFRAMES=1, needs `pnpm hyperframes:image`): HyperFrames determinism
pnpm test:matrix     # the scene matrix (needs browser:ensure); SCENE_MATRIX_ASPECTS=16:9 limits it
```

The golden suite renders, for every scene, its illustrative trace at each
aspect (`illustrativeCases` in `tests/golden/cases.ts`; the catalogue unit
test fails if a scene lacks an aspect). For S1 that is five frames in 16:9
and one each in 9:16 and 1:1, plus one frame per aspect of a replay with the
longest allowed source (a labelled test fixture), and at 9:16 and 1:1 a replay whose source is 120
"W"s and a trace with a 160-character title, all at native size (scale 1).

**Scene matrix** (`tests/matrix/scene-matrix.test.ts`, plan Phase 3's arming
gate; CI runs one `scene-matrix` job per aspect). For every registered scene
at each aspect it:

- renders the whole scene to mp4 through the adapter and reads the file back
  (native size, 30 fps, H.264, the timeline's length), and renders its poster
  frame as a still;
- mounts the scene in the embed test page with its illustrative trace, in the
  pinned Chrome Headless Shell, and checks that it plays;
- asserts the D2 mode label **on screen**: the text is the trace's own mode
  line (ILLUSTRATIVE; and, for the same events as an attested replay, REPLAY
  with date range and source); the badge and the detail line each lie inside
  the scene's frame, are visible and opaque, with opaque text that contrasts
  with its background; the label is the topmost scene element at its centre;
  and a screenshot of it shows drawn text;
- checks that an embed not asked to autoplay stays on the poster frame; that
  under `prefers-reduced-motion: reduce` an embed asked to autoplay with no
  controls stays on the poster frame, shows a visible play control, and plays
  only once it is pressed; that turning the preference on mid-play returns to
  the poster and turning it off plays again; and that a server-rendered,
  hydrated embed autoplays only once the client reports no preference.

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
