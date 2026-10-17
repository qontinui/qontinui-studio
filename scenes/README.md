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
  composition.ts  compositions -> a playable program (segments, narration warp, omitted b-roll)
  program-root.tsx  what an adapter renders for a program frame (scene, card or b-roll + mode label)
  narration-timing.ts  the narration timing table (kept apart from scene code)
  captions.ts     aligned words -> WebVTT captions
src/primitives/   node, edge, PR card, session lane, gate, timeline, counter, caption, mode label,
                  feed (a panel of short rows), scene header, label (a small pill on a point)
src/compositions/ the teaser, the explainer and the factory pulse (plan Phase 5)
src/gate/         the unattended publish gate (manifest, speech, layout, ffprobe, frames, audio, captions)
src/narration/    speak -> re-transcribe -> align (Kokoro-FastAPI, Whisper); vendor/explainroo (MIT)
narration/examples/  an example narration script (a production's own scripts live in its repo)
src/scenes/       the scenes of the plan's catalogue (S1 "Plan lifecycle", S2 "The digital twin",
                  S3 "Gates and continuations", S4 "The merge train", S5 "The fleet",
                  S6 "Self-correction", S7 "Model-based GUI automation")
src/adapters/     engines. remotion/ is the first adapter, hyperframes/ the tested fallback;
                  fonts.ts and output.ts are engine-neutral
docker/hyperframes/  the pinned HyperFrames render image (Dockerfile + npm lockfile)
docs/             hyperframes-spike.md: the HyperFrames adapter spike result
embed/            the embeddable package's public types (player-api.ts) and README (`pnpm embed:pack`, see below)
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
  a traversal starts where the previous one ended (re-taking an edge,
  revisiting a state and exploring after a traversal starts are allowed);
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
one PR, a closed loop (red, a fix, then green) and no operator step, S7 only graphs its layouts can
draw honestly: at most six states found, no self-loop, one transition per pair
of states, no edge running through another state and no action label
overlapping another or a state at any aspect; with states placed in discovery
order, a triangle among the first three states found cannot be drawn).
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

## Compositions (plan Phase 5)

A composition plays segments back to back: a window of a scene (at a playback
rate), a title or closing card, or an AI b-roll still in a named slot. The
registered ones are in `src/compositions/`:

| Composition | Segments | Length |
| --- | --- | --- |
| `teaser` | card, S1, S4, S6 at 2x, b-roll slot `teaser-closing`, card | about 30 s |
| `explainer` | card, S2, b-roll `explainer-twin`, S1, S4, b-roll `explainer-fleet`, S6, card | about 75 s silent; narration stretches it towards 90 s |
| `factory-pulse` | card `{{week}}`, S1, S6, S4 at 1.5x, card `{{status_line}}` | about 40 s |

```sh
pnpm studio-scenes compose explainer --format mp4 --aspect 16:9 --out out/explainer.mp4 \
  [--trace <segment>=<file>]... | [--build-record <export>] \
  [--broll <slot>=<image>]... [--narration <timing.json>] [--var <name>=<value>]... [--force]
pnpm studio-scenes compositions
```

- **Scenes are not changed.** A scene segment renders through `SceneRoot`,
  exactly as a lone render does, so its D2 mode label comes with it. Cards
  and b-roll carry the mode label of their nearest scene. Card copy quotes
  the served `product_intent/vision` (`src/compositions/copy.ts`, plan D5).
- **Traces.** Each scene segment uses its scene's illustrative trace unless
  `--trace <segment>=<file>` names another. `--build-record` makes every
  scene segment a replay through `fromBuildRecord`; while replays are
  unavailable it exits **5** (not 2), so a scheduled caller can fall back
  to an illustrative render and say so.
- **B-roll is optional.** A slot with no `--broll` image is omitted and the
  omission is recorded, so every composition renders with zero b-roll. An
  image needs the studio's `<image>.provenance.json` beside it
  (`ai_generated: true`, a `provider.id`, and `file.sha256` equal to the
  image's). Every b-roll frame carries an "AI-GENERATED IMAGERY" badge
  (sibling plan D6).
- **Narration is optional.** `--narration` takes a timing table
  (`src/api/narration-timing.ts`): per segment, a clip beside the table (by
  name and SHA-256; a clip that does not hash is refused), its length, its
  `[#mark]` times and, optionally, the aligned words and the independent
  speech check. Re-voicing writes a new table and never edits a scene. A
  narrated segment lasts at least its clip plus a 250 ms lead and a 500 ms
  tail; the scene plays at its rate and then holds its last frame. A mark
  named after an event id of the segment's trace pins that event's beat to
  the moment the mark is spoken (a piecewise-linear warp between pins); a
  mark that names nothing is ignored and noted, and marks spoken out of the
  scene's order, or so early or late that the scene would have to run
  faster than 4x or slower than 0.25x to meet them, are refused. A word that
  ends past its clip is refused by the table's schema (a narration lead of
  250 ms means a mark spoken in the first quarter-second will usually ask
  for more than 4x; speak a few words before the first mark), and a caption never
  outlasts its clip.
- **Outputs.** `<out>` (mp4/H.264 or webm/VP8, with the narration mixed in
  by the engine), `<out>.composition.json` (the manifest the publish gate
  reads: each segment's place and mode, what was omitted and why, notes, and
  the SHA-256 of every trace, image, clip and the output), and, when the
  narration carries words, `<out>.vtt` (WebVTT captions, cues split at
  sentence ends, silences and 42 characters). Existing outputs are refused
  without `--force`. The manifest and captions are written inside the
  same interruptible step as the render, so a first Ctrl-C lets them be
  written; a second signal (which exits at once) or a failed write can
  still leave a video without a manifest, and the publish gate then
  refuses it. A
  `--force` run with no captions leaves an older `<out>.vtt` in place (it
  deletes nothing it did not write) and says so; the manifest is the
  authority on whether captions exist.
- **Engine.** Compose renders through the Remotion adapter
  (`EngineAdapter.renderComposition`, optional on the interface; the
  HyperFrames adapter does not implement it yet). Media is staged into a
  fresh directory for that bundle and removed file by file after bundling,
  also when bundling fails; a media name colliding with a bundled asset is
  refused.
- **Reproducible.** Program resolution is pure, and a composed mp4 with a
  b-roll still and a narration clip is byte-identical across two runs
  (`tests/golden/compose.test.ts`).

## Narration (plan Phase 5)

```sh
STUDIO_TTS_URL=http://<host>:8880 STUDIO_ASR_URL=http://<host>:<port> \
  pnpm studio-scenes narrate teaser --script narration/examples/teaser.md --out out/teaser-voice
pnpm studio-scenes compose teaser --format mp4 --aspect 16:9 --out out/teaser.mp4 \
  --narration out/teaser-voice/teaser.timing.json
```

The loop is explainroo's (`src/narration/vendor/explainroo/`, MIT, vendored
with its licence and a list of what was changed):

1. **Speak.** The script has one `## <segment-id>` section per narrated
   segment. Each section is cut into sentences; each sentence is spoken by
   **Kokoro-82M through Kokoro-FastAPI** (`POST /dev/captioned_speech`), and
   the clips are joined, with the script's pauses, into one clip per segment.
2. **Align (seed).** The script's words are aligned, number- and
   name-aware, against Kokoro's own word timestamps, so every word and every
   `[#mark]` gets a time in its clip.
3. **Re-transcribe (check).** The clip is transcribed by an
   **OpenAI-compatible Whisper endpoint** (`POST /v1/audio/transcriptions`,
   `verbose_json`, word timestamps) and aligned against the script again.
   The share of script tokens heard is the segment's speech match (the
   publish gate needs 0.9). With no recogniser the check is recorded as
   `unavailable`, which the gate refuses.

Cue grammar: `[#name]` marks a moment (named after an event id of the
segment's trace, it pins that beat when composed); `[pause 0.6]` adds
silence; `{shown|spoken}` puts `shown` in the captions and makes the voice
say `spoken`; `<!-- -->` and `>` lines are not spoken.

The result is `<out>/<segment>.wav` per section and
`<out>/<composition>.timing.json`, the timing table `compose --narration`
reads (`src/api/narration-timing.ts`). It is separate from scene code, so
re-voicing (another voice, another script) never edits a scene.

**Services, by HTTP only.** Kokoro's Python stack links GPL code
(phonemizer, espeak-ng), so it runs as a separate service and only its audio
enters a render; this package never links it (plan Phase 0). Configuration:

| Variable | Meaning |
| --- | --- |
| `STUDIO_TTS_URL` | Kokoro-FastAPI base URL (`--tts-url`). Unset: `narrate` exits **6** and writes nothing. |
| `STUDIO_TTS_VOICE` | Kokoro voice (`--voice`), default `af_heart` |
| `STUDIO_ASR_URL` | OpenAI-compatible transcription base URL (`--asr-url`). Unset: the check is `unavailable`. |
| `STUDIO_ASR_MODEL` | model name sent to it (`--asr-model`), default `whisper-1` |
| `STUDIO_TTS_API_KEY_FILE`, `STUDIO_ASR_API_KEY_FILE` | a file holding a bearer key, when a service needs one (never a flag) |
| `STUDIO_SPEECH_TIMEOUT_MS` | per-request timeout, default 120000 |

A service URL may not carry credentials or a query (they would end up in
the timing table and in logs); a key goes in the key file. Exit codes: **6**
when the TTS service is missing, unreachable, failing or answering with
something unusable (unreadable key file, audio that is not 16-bit PCM WAV),
so a scheduled caller can render without narration and say why (a missing
or failing ASR service does not fail `narrate`: the check is recorded as
`unavailable`, and the publish gate refuses it); **2** for a
script or usage problem (an unknown `## <segment-id>`, a malformed cue, a
repeated mark, a section without words). The table is validated before it
is written, so `narrate` never writes one that `compose` would refuse; a
speech match is floored, never rounded up past the gate.

`narrate` writes only into an absent or empty directory, or (with
`--force`) one holding only its own files. The tests run the loop against
shape fixtures (`tests/fixtures/narration/`) through a fake `fetch` and a
loopback server; no live service is needed.

## The publish gate (plan Phase 5)

```sh
pnpm studio-scenes gate out/teaser.mp4 [--min-speech 0.9] [--max-silence 10]
```

Reads `<video>.composition.json`, runs every check, writes
`<video>.gate.json` (each check, pass or fail, and why) and exits **0** when
the video may be published unattended, **3** when it may not. Every check
runs even after one fails, and a check that cannot be measured fails
(UNKNOWN never passes).

| Check | Passes when |
| --- | --- |
| `manifest` | the video hashes to the manifest; every input (traces, b-roll, timing table, clips) still hashes to it; the program rebuilt from them places every segment where the manifest says |
| `speech` | every narration clip the REBUILT program plays (the manifest's own list must agree) has an independent speech check (Whisper vs script, from the timing table) of **≥ 0.9**; `unavailable` or never checked fails |
| `layout` | at three points in every segment: no visible text runs off the frame, is cut off by a clipping box with no ellipsis (horizontally past it, or more than a fifth of a line's height outside it), is partly covered by an opaque box, or overlaps other text; and every sampled frame has visible text (at least its mode label). Text shortened with an ellipsis by the box that cuts it (`text-overflow: ellipsis` horizontally, a line clamp vertically) is a **warning** on the report, not a failure: it is how the scenes fit long names at narrow aspects. Every clipping box is checked, so an ellipsized label that also runs off the frame fails |
| `ffprobe` | one video stream of the manifest's codec, size and frame rate, 8-bit 4:2:0; the container AND the video stream itself as long as the manifest says (within two frames; one frame for the stream's frame count); with narration, one audio stream |
| `frames` | four frames, at the middles of the four quarters, none black (nothing lit: 99.9th-percentile luma ≤ 40) or flat (luma s.d. < 1) |
| `audio` | there is an audio stream, it is not silent (mean ≥ −60 dB), no silence of 10 s or more (−50 dB), no clipping (peak < −0.1 dBFS) |
| `captions` | the captions file is byte for byte the one the rebuilt program and timing table produce (a stale or edited file fails), is valid WebVTT, its cue count matches the manifest, cues are ordered and end inside the video, and every narrated segment has a caption |

The layout check lays each sampled frame out, as static markup from the
same `ProgramRoot` every render draws, in the pinned Chrome Headless Shell
under the studio fontconfig, with the bundled fonts registered from their
bytes (a frame measured without its font would mean nothing, so a font that
fails to load fails the check). Its controls are in
`tests/golden/gate.test.ts`.

**What the gate does not see** (stated, so nobody relies on it): occlusion
is judged from background colours and images, so text under an opaque SVG
shape is not flagged; `clip-path` and `contain: paint` are not treated as
clips; a line-clamped box shorter than its clamp is reported as a
truncation warning, not a cut; and a frame showing only its mode label on
the dark canvas passes both `frames` and `layout` (missing scene content
with the label present is not detected). None of these patterns occurs in
the current scenes; the layout calibration over every composition, aspect
and 15th frame finds only designed truncations.

The last four checks are named after checks in calesthio/OpenMontage, which
is AGPL: they are this package's own implementation, and no code was taken
from it. ffprobe and ffmpeg come from `STUDIO_FFPROBE` / `STUDIO_FFMPEG`, else
`PATH`.

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
- **explainroo** (`src/narration/vendor/explainroo/`): MIT, Copyright (c)
  2026 Vincent Schmalbach. `align.js` and `script.js` are vendored from commit
  `025809e3`, modified as their headers list; the licence is in `LICENSE`
  beside them.
- **Kokoro-82M / Kokoro-FastAPI, Whisper**: not dependencies. They are HTTP
  services; only their audio and transcripts reach this package.
- **Inter** (`assets/fonts/`): SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
  Inter 4.1, full character set (latin, latin-ext, Greek, Cyrillic, Vietnamese,
  arrows and symbols; no CJK), from `inter-ui@4.1.1` (`web/Inter-*.woff2`,
  "Version 4.001;git-9221beed3"). The copyright line in `OFL.txt` is the one in
  the fonts' own name table ("Copyright 2016 The Inter Project Authors").

## The embeddable package (plan Phase 4)

`pnpm embed:pack [--out <dir>]` builds `out/embed/qontinui-studio-scenes-<version>.tgz`.
That is the npm tarball a website vendors and installs as a file dependency,
because the package is not published to npm. It contains:

- `dist/player.js`: `src/adapters/remotion/embed.ts` bundled into one ESM
  module, with React and Remotion left external (peer dependencies);
- `dist/player.d.ts`: a copy of `embed/player-api.ts`, which
  `tests/unit/embed-types.test.ts` holds to the source;
- the fonts;
- each scene's 16:9 poster frame, rendered at scale 1;
- `manifest.json`, `LICENSE`, `THIRD_PARTY_LICENSES` (bundled zod) and
  `README.md` (from `embed/README.md`).

The tarball is reproducible for a given toolchain: one commit, packed with the
same Node version and its bundled npm, gives one SHA-256. The CI job pins the
exact Node version. Every poster must be a golden frame with a matching hash,
or the pack fails. The CI `embed-package` job packs twice, requires identical
hashes, writes the hash to the job summary and uploads the tarball. A consumer records the studio commit
and the SHA-256 next to its vendored copy (qontinui-web:
`frontend/vendor/studio-scenes.pin.json`).

