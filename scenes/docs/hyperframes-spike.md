# HyperFrames adapter spike

Plan `2026-10-09-qontinui-marketing-studio-animated-process-explainers`, Phase 2,
bullet "HyperFrames adapter spike" (scout addendum 2026-10-09). Run on
2026-10-10/11 by session 4409f0d3, time-boxed. Measured on the shared Debian 13
development host (48 cores, load average 60–140 during the runs). Not legal advice.

## Verdict: CONFIRMED FALLBACK

HyperFrames renders the unchanged S1 scene through the studio API. No file
under `src/scenes/**`, `src/primitives/**` or `src/api/**` changed. The pinned
Docker image produces byte-identical PNG frames:

- between runs;
- between worker counts;
- in reversed order;
- for a frame rendered alone.

Two mp4 renders are also byte-identical.

The plan's condition for a confirmed fallback ("identical frames with no scene
changes") is met. One renderer defect had to be worked around **in the
adapter**, not in the API or a scene. The adapter mounts a fresh React tree per
frame (see "Determinism"). Without that, frames depended on render history.

**Promote to primary: no, not now** (see the last section). Remotion stays the
first adapter, and HyperFrames is the tested, opt-in second adapter.

## What was built

| Path | What |
| --- | --- |
| `src/adapters/hyperframes/frame-adapter.tsx` | A HyperFrames `FrameAdapter` (type from `@hyperframes/core`). `seekFrame(f)` renders `<SceneRoot key={f} frame={f}/>` inside `flushSync`. |
| `src/adapters/hyperframes/page.tsx` | The browser entry. It reads the embedded render config, loads the bundled Inter and draws frame 0, and only then registers the adapter with the HyperFrames runtime. It also has a playback clock for `<hyperframes-player>`. |
| `src/adapters/hyperframes/project.ts` | Writes a self-contained HyperFrames project: `index.html`, `studio-scene.js` (`page.tsx` bundled by esbuild) and `fonts/`. Like every render path, it refuses a replay without build-record provenance. |
| `src/adapters/hyperframes/render.ts` | `HyperFramesAdapter implements EngineAdapter` (`render`, `renderStills`, `close`). It runs the pinned image by id in a hardened, limited container with no network. |
| `src/adapters/hyperframes/image.ts`, `scripts/hyperframes-image.ts` | Image identity: the inputs-hash label, the reference check and the labelled build (`pnpm hyperframes:image`). |
| `docker/hyperframes/` | The pinned render image: `Dockerfile`, `package.json`, `package-lock.json`. |
| `src/cli/main.ts` | `--engine remotion\|hyperframes` (default `remotion`). The adapter is loaded by dynamic `import()`, as before. |
| `tests/golden/hyperframes.test.ts` | Determinism test, opt-in: `pnpm test:hyperframes` (`HYPERFRAMES=1`). |
| `lint-fixtures/**/*hyperframes*` | 12 fixtures that prove the engine ban covers HyperFrames in scenes, primitives, the API and the CLI. |
| `tests/unit/engine-graph.test.ts` | HyperFrames packages (`hyperframes`, `@hyperframes/*`, `@heygen/*`) are in the forbidden set, and a live control proves the check fires. |

`pnpm hyperframes:image` builds the image. Then:

```sh
pnpm studio-scenes render s1-plan-lifecycle --trace traces/illustrative/s1-plan-lifecycle.json \
  --engine hyperframes --format mp4|webm|gif|png --aspect 16:9|9:16|1:1 --out <path> [--frame <n>]
```

Formats, as rendered through the CLI:

- **png:** one still with `--frame`, or the 426-frame sequence as
  `element-000.png …`, the same names the Remotion adapter writes.
- **mp4:** H.264.
- **webm:** VP9. Remotion writes VP8.
- **gif:** 213 frames at 15 fps, every second scene frame (Remotion does the same).

Only scale 1 is supported.

### How the scene reaches HyperFrames

The pinned 0.8.78 runtime drives whatever is registered at
`window.__timelines[<composition id>]`. Its own source allows a partial entry:
"duration/seek only … render fine" (`packages/core/src/runtime/init.ts`). The
page registers the frame adapter as `{ duration, time, seek, play, pause, paused }`.

- `seek(t)` maps seconds back to the frame index HyperFrames derived them from
  (`t = floor(frame) / fps`, so `round(t * fps)`).
- It then calls `seekFrame`, which commits the React render synchronously
  before returning. HyperFrames captures afterwards.

HyperFrames v0.8.140 (2026-10-07, PR #5130) added
`window.__hyperframes.registerFrameSource`, the documented API for driving
"scene code that exposes a function of time". It is too new for the pin (see
"Versions"). Moving to it changes only `page.tsx`.

`renderStills` renders one short composition whose i-th frame is the i-th
requested scene frame, because the HyperFrames CLI has no single-frame mode.
GIF uses the same mapping at 15 fps.

## D7 seam findings

**No change to the studio API was needed.** `EngineAdapter`, `RenderRequest`,
`StillsRequest`, `SceneRoot` and the engine-neutral `adapters/fonts.ts` and
`adapters/output.ts` were used as they are. The Remotion adapter, every
existing test and the goldens are untouched (verification below).

Findings, none of which required an API change in this spike:

1. **`PlayerEmbedProps` is shaped like Remotion's player.** It expects an
   in-process React component that receives the trace as a prop and validates
   it at view time. `<hyperframes-player>` takes the URL of a composition and
   loads it in an iframe, so the trace is baked into a published composition
   at build time. A HyperFrames embed would therefore be "published composition
   URL + poster", not a React component. The replay-provenance check (plan D2)
   would move from view time to publish time, which matches the Phase 3 caveat
   in `README.md` that a serialised trace loses its attestation. The contract
   should change only if HyperFrames is promoted (Phase 4). It is recorded here
   and not changed.
2. **Render history must not leak into a frame**, and the API does not say who
   guarantees that. Scenes are pure, but HyperFrames reuses one page across
   many frames, as Remotion does. Under its software compositing, React
   updating the DOM in place made antialiased edges depend on earlier frames.
   That is the same class of defect as the blurred-glow cache in `src/api/glow.ts`. The fix belongs to the adapter (a remount per frame) because
   it depends on the renderer. The adapter contract could state "a frame must
   equal the same frame rendered alone" as an obligation on every adapter.
   Today only the golden suites enforce it.
3. **Scale and codec are adapter capabilities.** HyperFrames renders scale 1
   only, and its WebM is VP9 where Remotion's is VP8. `RenderRequest` lets the
   adapter refuse, which it does. The CLI also refuses `--engine hyperframes
   --scale ≠1` before rendering.

## Determinism

S1, illustrative trace, PNG frames at full size (1920x1080). Every comparison
was made both by file SHA-256 and by decoded RGB pixels.

| Comparison | Frames | File hash differs | Pixels differ |
| --- | --- | --- | --- |
| Run 1 vs run 2 (default 6 workers) | 426 | 0 | 0 |
| Run 1 vs run 2, 9:16 | 426 | 0 | 0 |
| 6 workers vs 1 worker | 426 | 0 | 0 |
| Full sequence vs reversed sequence (425 → 0) | 426 | 0 | 0 |
| Sequence vs the 5 golden indices in one composition | 5 | 0 | 0 |
| Sequence vs each golden index alone (fresh container) | 5 × 1 | 0 | 0 |
| CLI `--frame 196` still vs sequence frame 196 | 1 | 0 | 0 |
| mp4, two runs | 1 file | identical SHA-256 (`de714fb5…`) | — |

**Before the per-frame remount** (React updating the DOM in place):

| Comparison | Frames | Pixels differ |
| --- | --- | --- |
| Run 1 vs run 2, light load | 426 | 0 |
| 6 workers vs 1 worker | 426 | **336** (up to 115 px in a frame, up to 9 levels) |
| Same, in the test suite, heavier load | 426 | **355**, plus **1 frame between two runs** |
| mp4: 6 workers vs 1 worker, decoded frames | 426 | 382 frames' md5 differ (lossy: a difference spreads through its GOP, so this counts frames, not pixels) |

The differing pixels were antialiased edges of the rounded stage nodes
(`y = 306/316`) and of the timeline markers (`y ≈ 959`). Those are the elements
whose state changes between frames.

HyperFrames launches Chrome with `--disable-gpu-compositing` under SwiftShader.
Its own source explains why: compositor raster on SwiftShader goes stale (HF#3049,
Chromium 535256667). With in-place updates, the software re-raster of the changed
regions did not equal a full raster.

Mounting a fresh tree per frame (`key={frame}`) made every frame equal the
same frame rendered alone. The gated test proves this both ways. With the
remount it passes 9/9. With the remount removed it fails 7/9: run to run, worker
count, and all five order cases.

## Cross-engine parity (informative)

HyperFrames frames compared with the Remotion adapter's frames for the same
trace and aspect. On this host Remotion reproduced every committed golden
SHA-256 (`16x9` f0, f48, f196, f290, f425 and `9x16` f250). No HyperFrames frame
matches its golden hash.

| Frame | Pixels differing | Share | Max channel Δ | Mean Δ (differing px) | pixelmatch t=0.1 |
| --- | --- | --- | --- | --- | --- |
| 16:9 f0 | 23,646 | 1.14 % | 163 | 26.4 | 5,001 |
| 16:9 f48 | 42,312 | 2.04 % | 163 | 15.4 | 5,005 |
| 16:9 f196 | 88,289 | 4.26 % | 238 | 10.6 | 5,481 |
| 16:9 f290 | 87,465 | 4.22 % | 231 | 10.8 | 5,588 |
| 16:9 f425 | 85,003 | 4.10 % | 231 | 11.1 | 5,588 |
| 9:16 f250 | 90,769 | 4.38 % | 231 | 9.2 | 3,908 |

The differences fall into three groups:

1. **±1 level in translucent fills** (for example stage node `rgb(26,57,41)` against
   `rgb(26,56,41)`). These come from the two compositing paths: Remotion uses
   GPU compositing on SwiftShader (`swangle`), HyperFrames uses software
   compositing. They account for most of the count and are invisible.
2. **Some text lines sit 1 px higher in HyperFrames**, for example the mode
   label's detail text, where the best alignment is `dy = -1`. Titles align
   at `dy = 0`. The large maximum deltas come from glyph edges shifted by one
   pixel.
3. **Antialiased edges** of rounded corners and markers.

Side by side at full size the frames look the same: same layout, colours, text
and mode label, at both aspects. The diff images (red = any difference,
threshold 0) are in `docs/hyperframes-spike/parity-*.png`.

Parity was not required, and the two engines cannot share goldens. Each would
need its own goldens.

## Render time

The full S1 sequence (426 PNG frames, 16:9) on the shared host. Load is the
1-minute load average when each run started.

| Engine | Run | Wall time | Load |
| --- | --- | --- | --- |
| HyperFrames (6 workers, includes container start; measured with the spike's original `docker run`) | 1 | 11.3 s | 84 |
| | 2 | 12.8 s | 95 |
| | 3 (with remount) | 16.7 s | 113 |
| | 4 (with remount) | 10.2 s | 77 |
| | 5 (with remount) | 10.9 s | 106 |
| HyperFrames, 1 worker | | 43.7–45.4 s | 91–112 |
| HyperFrames, 9:16 | 2 runs | 14.9 / 18.2 s | 92–109 |
| HyperFrames mp4 (BeginFrame capture, streaming encode) | 2 runs | 21.1 / 32.7 s | 97–138 |
| Remotion CLI (`render --format png`, includes bundling) | 2 runs | 60.5 / 35.8 s | 107–109 |
| Remotion CLI, 9:16 | 1 | 45.6 s | 133 |

HyperFrames was about 3–5× faster here. This is not a tuned comparison. Each
engine used its default concurrency, and on a host this loaded the variance is
large. For the weekly pulse, either engine is fast enough.

## The `<hyperframes-player>` embed

The check used a static host page:

```html
<hyperframes-player src="comp/index.html" poster="poster.png" width="1920" height="1080">
```

`comp/` is a project written by `project.ts`. The page was driven by puppeteer
inside the pinned image with `--network none`.

| Check | Result |
| --- | --- |
| `ready` | Yes, `duration` 14.2 s (426 frames). The player found our timeline directly (its "direct timeline adapter" path). |
| `seek(196/30)` | `currentTime` 6.533. After the first play, the frame shown was frame 196, scaled into the 960x540 box. |
| `play()` / `pause()` | Advances: 1.87–1.90 s after 2 s, driven by the page's playback clock. The clock picks the frame; the frame is still a pure function of `(trace, frame)`. |
| `poster` | Shown before the first play, but at its natural size (a 1920 px poster is cropped in a 960 px box), and a `seek` alone does not remove it. A poster made at display size, or CSS, would fix this. |
| Network | Only the host page, the player script, the composition, its bundle and its fonts were requested. The player loads the HyperFrames runtime from jsDelivr only as a fallback when no timeline appears within about 1 s, and it did not fall back. A production embed should self-host that runtime or make sure the fallback cannot fire (privacy and CSP). |
| Isolation | The composition runs in an iframe with `allow-scripts allow-same-origin` (Chrome warns about that pair). Style isolation from the host page is complete. |

So the player can embed our scene with seek, play and a poster. The trace is
fixed when the composition is built (see D7 finding 1).

## Versions and pins

| Item | Pin | Why |
| --- | --- | --- |
| HyperFrames CLI (`hyperframes`) | **0.8.78**, npm, published 2026-09-26T03:15Z, `sha512-…` in `docker/hyperframes/package-lock.json` | The newest release at least 14 days old when the spike started (2026-10-10). The repo had tagged `v0.8.145` the same day, roughly one release per hour on busy days, so pinning matters. |
| `@hyperframes/core`, `@hyperframes/player` | **0.8.78**, exact `devDependencies` | `core` provides the `FrameAdapter` type (erased at build). `player` is used by the static embed check and by the graph test's live control. |
| Base image | `node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392` | Upstream uses the floating tag. |
| Debian packages | `snapshot.debian.org` at `20260925T000000Z` (ffmpeg 5.1.9, fontconfig, the Chrome libraries) | Upstream uses the live mirror. |
| Browser | chrome-headless-shell **149.0.7790.0** (Chrome for Testing), downloaded from the official CfT URL and checked at build against the committed SHA-256 `a3b011ab4c726e215cdeb623907a09cfb48f07054a7271fdda555ee2ae4f804d` | The same Chromium version `@remotion/renderer` 4.0.529 pins (`TESTED_VERSION`), so engine differences are not browser differences. Upstream uses `@stable`, unverified. The hash was computed on 2026-10-11 from the official URL, before install; the zip's MD5 matched the bucket's own `x-goog-hash`. |
| npm tree | `npm ci` from the committed lockfile | Upstream runs `npm install -g hyperframes@<v>`, which floats every transitive dependency. |
| Built image | `studio-hyperframes:0.8.78`, id `sha256:8aac16e543dc1899882e040dc73534efdb81a7b224ecec367ca06318fce4a8d2`, label `org.qontinui.studio.inputs-sha256=840e3d688f3ae410d1df90f255da54326686c2badb210ce17fed25aef7513186`, user `studio`, entry point `studio-hf-render` | The current image, after three rounds of review fixes (round 3 added the container deadline in `studio-hf-render.sh`; the previous id was `sha256:e9fed5e3d72980f2709a40f0b8653b4d0fbdd647ad53b6edfe43af1e80d4e687`). The spike's measurements used the first build, `sha256:bf9b3f51ca8a4bc382086dc6e78412a5ab40e9705893b49b89b0e047858681c1`. Its Chrome came from `@puppeteer/browsers` at the same version, without a hash check. The current image reproduces that build's 426 frames byte for byte. |

**About "the pinned Docker image".** HyperFrames publishes no render image.
`hyperframes render --docker` builds `hyperframes-renderer:<version>` locally
from `packages/cli/src/docker/Dockerfile.render`, which pins neither the base
image, the browser (`chrome-headless-shell@stable`), the apt packages nor the
transitive npm tree. So two people running "the same version" on different
days get different images. `docker/hyperframes/Dockerfile` pins each of those
inputs by version or digest, and checks it by hash:
- the base image by digest;
- apt packages from a dated snapshot, through Debian's signed Release files;
- Chrome against a committed SHA-256;
- npm through the lockfile's `integrity` hashes.

**Image identity.** `pnpm hyperframes:image` (`scripts/hyperframes-image.ts`)
labels the image with a SHA-256 of `docker/hyperframes/{Dockerfile,
package.json, package-lock.json, studio-hf-render.sh}` (`src/adapters/hyperframes/image.ts`). The
adapter checks the image before every render:
- it recomputes that hash and refuses an image whose label differs, or that
  has none, with a hint to rebuild;
- it then runs the image by its resolved `sha256:` id, not by its tag;
- `STUDIO_HYPERFRAMES_IMAGE` must match an image-reference pattern, and a
  value starting with `-` is refused.

**Container.** Each render runs in its own container:
- **Name and lifecycle:** each render's container gets a unique name
  (`studio-hf-<random>`) and two labels: `org.qontinui.studio.run` (this
  render) and `org.qontinui.studio.adapter` (this adapter).
  - It is made by `docker create --rm --init` and run by
    `docker start --attach`, as separate client calls.
  - Every docker client is spawned `detached`, in its own process group. So
    a terminal Ctrl-C, which goes to the whole foreground group, reaches only
    node, and the adapter's `close()` drives the teardown. It never kills a
    client in the middle of `create`.
  - On timeout the adapter runs `docker kill <name>`, then kills the client.
  - After every render, in a `finally`, a sweep removes the render's
    container by label and by name with `docker rm -fv` until a listing shows
    none.
    - When `create` did not exit cleanly (killed by its own timeout, failed,
      or could not be spawned), the sweep keeps looking for 10 s even while
      it finds nothing. The daemon can finish a create after its client is
      gone.
    - Every docker call on a cleanup path (`kill`, `rm`, `ps`, `inspect`) is
      bounded to 15 s. Inside a sweep each call is also cut at the sweep's
      60 s deadline, so 60 s is the real bound on a sweep (plus the moment
      it takes to kill a docker client).
  - The container renders under `timeout` set to the adapter's render
    timeout plus 60 s (`STUDIO_HF_DEADLINE_SECONDS`, in
    `studio-hf-render.sh`), so a container nobody manages any more still
    ends.
  - `close()` marks the adapter closed, so a render stops at its next phase
    and a container created after close is never started. It also kills
    running containers, waits for the renders to unwind, and then sweeps by
    the adapter label.
- **What is proven:**
  - **Round 2.** The reviewer's reproduction (a 4 s timeout at load ~100,
    with the client killed between create and start) leaves no container.
    Timeouts of 100 ms to 8 s were run for png and mp4, 46 renders.
  - **Round 3, Ctrl-C during `create`** (the reviewer's method), load ~27.
    The CLI (`node bin/studio-scenes.mjs`) ran under `setsid`, and SIGINT
    went to its whole process group 0 s to 8 s after `docker create`
    started. A `docker` shim on `PATH` announced each `create` over a FIFO
    and then `exec`ed the real client, so "0 s" is the moment the create
    client starts. 22 runs: 13 png stills (0–3 s) and 9 mp4 (0–8 s), the
    last 5 re-run on the final code.
    - Every run exited 130 with "the render was stopped; nothing was
      published to …".
    - Every `create` client was in its own process group (its pgid was its
      own pid, never the CLI's).
    - `docker events` (label filter) showed 22 creates and 22 destroys; 13
      of those containers were removed without ever being started.
    - No run left a container (checked at exit and 3 s later) or an entry
      in the output directory. The project-directory check of these runs
      looked in `/tmp` while `$TMPDIR` pointed elsewhere, so it proved
      nothing; the re-review's runs below check `$TMPDIR`. One project
      directory found there afterwards was traced by its timestamps to the
      tsx run described next, which exited without cleanup.
  - **Round 3, the same under the tsx CLI** (`tsx src/cli/main.ts`, how
    `pnpm render` used to run). 6 runs (png and mp4, 0–5 s): 5 exited 130
    as above; in 1 the render had finished before the signal and exited 0
    with its output complete. None left a container. The first attempt of
    this round found that tsx's parent re-sends SIGINT to its child when the
    child does not acknowledge it within 30 ms, which, under load, hit the
    CLI's "second signal exits at once" path and skipped cleanup (one run
    left its staging directory and exited before its container was gone).
    Fixed twice over: `pnpm render` / `pnpm studio-scenes` now run
    `bin/studio-scenes.mjs` in one process, and the CLI treats a repeat
    within 1 s as the same interrupt.
  - **Round 3, create-timeout path.** `createTimeoutMs` of 1 ms to 600 ms,
    12 runs, each rejected ("could not create the HyperFrames container")
    after 10.1–12.2 s, i.e. after the 10 s late-create window.
    - `docker events` showed 8 creates and 8 destroys: in 8 of the 12 runs
      the daemon finished the create after its client had been SIGKILLed,
      and the sweep removed the container.
    - No container was left, at rejection or 3 s later.
  - **Round 3, `close()` at several delays** after `render()` or
    `renderStills()` started, by test (`tests/golden/interrupt.test.ts` for
    Remotion, 9 cases; the gated `hyperframes.test.ts`, 7 cases). Each
    rejects with `AdapterClosed`. A render or sequence leaves its output
    directory empty, nothing in it is created or renamed after `close()`,
    and no labelled container is left. A `renderStills` call may keep the
    stills it published before `close()`. The Remotion cases run each in a
    child process with no rejection listener, so they also fail if the
    process crashes; the file passed 6 runs in a row on the final code.
    Writing that test found four Remotion defects, all fixed:
    - `close()` itself could reject: Remotion's `browser.close()` kills
      Chrome and then queries its pages over the dead connection ("Target
      closed"). In the CLI that would have exited 1 instead of 130. The
      rejection is now ignored (Chrome is already gone).
    - A png sequence could leave its staging directory: Remotion rejects at
      once on cancel while frames it had started are still being written.
      A cancelled render call now waits until `close()` has closed the
      browser before its staging cleanup runs, and the cleanup retries
      briefly if a last plain file lands after its first pass.
    - Remotion abandons promises on cancel that later reject with nobody
      awaiting them: `renderStill`'s un-awaited page cleanup ("Target
      closed") and `renderFrames`' un-awaited server cleanup (a raw EPIPE
      from the compositor's stdin). The re-review (R2) rejected the first
      fix, a shape-matching process listener, because it could swallow a
      caller's own rejections and overrode `--unhandled-rejections`. Now
      every render call runs in an `AsyncLocalStorage` context, and the
      listener absorbs only rejections Node reports from inside a
      cancelled render of that adapter; anything else gets Node's own
      behaviour for the active mode (README, "Remotion and SIGINT"; unit
      tests cover inside, outside, and the throw / warn /
      warn-with-error-code / none modes). Before the fix, with the listener
      disabled, 3 of 27 child runs crashed; after it, 0 of 48.
    - A still cancelled through Remotion could leave its file server open,
      keeping the process alive (3 of 24 child runs). Stills are no longer
      given the cancel signal; `close()` lets a still in flight finish
      (bounded by 15 s) before closing the browser, and the result is not
      published.
  - **Round 3, Remotion through the CLI**, final code. SIGINT to the CLI's
    group 0.5–12 s after start, mp4, 6 runs: each exited 130 with "the
    render was stopped; nothing was published", and left nothing in the
    output directory. In 3 more (4, 7, 12 s) every one of the 12 Chrome
    processes the CLI had started was gone 2 s after exit.
  - **Round 3, a Remotion library caller with no SIGINT handler** (default
    options), SIGINT to its group at 7 s and 12 s, twice each: it exited
    130, all 12 Chrome processes it had started were gone 2 s later, and
    nothing was published. Its staging directory is left (no cleanup runs in that mode;
    see README).
  - **Round 3 re-review (R1), SIGHUP.** The reviewer's method: CLI under
    `setsid`, SIGHUP to its group 0–9 s after `docker start` began (the
    shim announced `start` instead of `create`), 11 runs (8 mp4, 3 png).
    Every run exited 129 with "the render was stopped; nothing was
    published", 0.2–0.6 s after the signal. `docker events`: 11 creates,
    11 kills, 11 destroys. No container at exit or 3 s later, nothing in
    the output directory, no project directory in `$TMPDIR`.
    - A first batch of 7 found one more gap: with SIGHUP right as `docker
      start` began, `docker kill` hit a container that was not running
      yet, failed, and the render then ran to its end (30 s) before the
      CLI could exit, unpublished. `close()` now retries the kill until it
      lands or the render has let go of the container (at most 15 s).
    - SIGHUP during `create`: 3 runs, all exit 129, nothing left.
  - **Round 3 re-review, SIGTERM.** During `start`, 5 runs (mp4 and png,
    0–4 s): all exit 143, nothing left. SIGINT during `create` re-run with
    the `$TMPDIR` check: 6 runs, all exit 130, nothing left.
  - **Round 3 re-review, Remotion CLI.** SIGHUP at 3, 6, 10 s: exit 129;
    SIGTERM at 4, 8 s: exit 143. In all 5, all 12 Chrome processes were gone
    2 s after exit, nothing was published and no staging directory added.
  - **Round 3 re-review, container deadline.** The image run directly with
    `STUDIO_HF_DEADLINE_SECONDS` of 1, 2 and 3 on a full 426-frame mp4
    project: each exited 124 (timeout) after 2.0–3.6 s, wrote nothing to
    `/output`, and was removed by `--rm`. A bad value is refused. Frame 200
    rendered by the rebuilt image is byte-identical to the same frame
    rendered by the previous image earlier this round (the previous image
    itself was no longer on the host for a full comparison); the gated
    determinism test passes on the rebuilt image.
  - **Not proven:** anything after a SIGKILL of node itself. Then nothing is
    left to clean up the staging and project directories; the container
    still ends at its own deadline.
- **Output:** the image renders into its own tmpfs and copies only the
  finished artifact to `/output`, first as `.partial-<name>` and then renamed
  to `<name>` (`docker/hyperframes/studio-hf-render.sh`). HyperFrames
  otherwise writes its `work-*` and `.<name>.hf-transaction-*` trees beside
  `--output`, inside our staging directory. After a failure the adapter
  removes `<name>` and `.partial-<name>`, which are a file or a directory of
  plain files, one level deep.
- **Interrupts:** the CLI handles SIGINT, SIGTERM and SIGHUP. SIGHUP was
  added in round 3: with every docker client in its own process group, a
  terminal hang-up no longer reaches the container through its client, and
  the CLI died at once (129) leaving the container rendering for minutes
  (the round-3 re-review's finding R1).
  - It calls `close()` on the adapter, which is single-use after `close()`
    (`AdapterClosed`) and never publishes after it. The render unwinds
    through its own staging, project and container cleanup.
  - The CLI exits 130 (INT), 143 (TERM) or 129 (HUP) and says which happened: stopped
    (or failed while stopping) with nothing published, or already finished
    with the output complete. A second signal a second or
    more later exits at once and skips cleanup; an earlier repeat is taken
    as the same Ctrl-C relayed by a wrapper (pnpm, the tsx CLI).
  - On Remotion, `close()` cancels through `makeCancelSignal`, always. It
    waits for a `selectComposition` in flight before closing the browser,
    because closing the browser under it makes Remotion leak its file
    server (the review's info item).
  - Remotion's launcher adds a SIGINT listener that kills Chrome and calls
    `process.exit(130)` at once. By default (`interrupts: "remotion"`) it
    stays, so a library caller with no handler still kills Chrome on Ctrl-C.
    The CLI passes `interrupts: "caller"`: the adapter then removes exactly
    that listener after each launch, matched by its source, and touches no
    other listener (README, "Remotion and SIGINT").
  - Remotion's `close()` closes the browser (after any `selectComposition`
    in flight), then waits at most 15 s for the cancelled renders to finish
    their staging cleanup, and never rejects. While the CLI is stopping, any
    stray engine rejection the adapter did not absorb is reported as a note
    instead of crashing past the cleanup.
- **Isolation:** `--init`, `--network none`, `--read-only` with a tmpfs `/tmp`
  (4g by default; the only path HyperFrames and Chrome write besides
  `/dev/shm` and `/output`),
  `--cap-drop=ALL` and `--security-opt no-new-privileges`. The image's default
  user is `studio`; the adapter runs as the caller's uid:gid so the output
  files belong to the caller.
- **Limits:** `--pids-limit` 8192, `--memory` 8g, `--cpus` 8 and a 4g tmpfs.
  They can be overridden by `STUDIO_HF_PIDS_LIMIT`, `STUDIO_HF_MEMORY`,
  `STUDIO_HF_CPUS` and `STUDIO_HF_TMPFS`, which are validated.
  - **The tmpfs is memory.** Everything HyperFrames writes there (its work
    directory, the captured frames, the artifact before it is copied out)
    counts against `--memory` together with Chrome.
  - A 1080p S1 render needs well under 1 GB.
  - For long or 4K renders, raise `STUDIO_HF_TMPFS` (a 4K frame is about 4×
    a 1080p one) and raise `STUDIO_HF_MEMORY` by at least as much. Otherwise
    the container is OOM-killed instead of the tmpfs filling.
- **Mounts:** `--mount type=bind,…`, not `-v`. A host path containing a comma,
  quote or newline is refused, because docker's `--mount` cannot take one
  safely. `:` is safe there.

The gated test passes 9/9 under these settings, including `--init` and the
tmpfs output path, and the 426 frames are byte-identical to the spike's.

The built image id is local, because a rebuild elsewhere produces a different
id. Pinning by a digest that CI can pull needs the image published to a
registry (for example GHCR). That was not done in the spike, and it is why the
HyperFrames test is **not** in CI. The default `pnpm test:golden` and the
existing CI jobs are unchanged.

## Licence and telemetry

- **Licence.** HyperFrames is Apache-2.0. Each package states it differently:
  - the repository's `LICENSE`;
  - `@hyperframes/core` and `@hyperframes/player` ship a `LICENSE` file
    (Apache-2.0) in their npm tarballs, but **have no `license` field** in
    their package.json;
  - the `hyperframes` CLI declares `"license": "Apache-2.0"` but its tarball
    **ships no LICENSE file**.

  It is compatible with the MIT studio and with AGPL-3.0 qontinui-web,
  and needs no §7 additional permission. Distribution must keep the licence
  and any NOTICE.
- **CLI telemetry.**
  - The CLI sends PostHog events by default.
  - It is disabled by `HYPERFRAMES_NO_TELEMETRY=1` or `DO_NOT_TRACK=1`
    (`packages/cli/src/telemetry/policy.ts`), or persistently by `hyperframes
    telemetry disable`.
  - **Upstream's `--docker` path does not forward either variable into the
    container** (`packages/cli/src/utils/dockerRunArgs.ts`), so the CLI inside
    it falls back to its persisted config.
  - The studio sets both variables in the image (`ENV`) and again on every
    container it creates (`docker create -e …`), and runs the container with `--network none`, so nothing can
    leave it. Verified: `hyperframes telemetry status` in the image reports
    `disabled`, source `HYPERFRAMES_NO_TELEMETRY`.
- **Page runtime analytics.** The in-page runtime only `postMessage`s events to
  its host (`runtime/analytics.ts`) and makes no network calls.
- **Any CI job** added later must set both variables and keep `--network none`.
  The adapter does all three itself.

## Promote HyperFrames to primary?

**Recommendation: no, not now.** Keep Remotion as the first adapter, with
HyperFrames as the confirmed, opt-in fallback, and re-open the question when
either of these happens:

- one of Remotion's revisit triggers fires (headcount reaches 4,
  tenant-uploaded scene code, or qontinui-web cannot adopt the §7 permission);
- HyperFrames ships a stable frame-source API in a release past the 14-day
  pin window, and a published, digest-pinned render image.

The reasoning follows the served `engineering-priorities` order. Effort and
backward compatibility are not factors.

1. **Capability: a tie today, with one future edge each way.**
   - Both render every format and aspect from the same scenes.
   - HyperFrames' Apache licence has no headcount cliff and no restriction on
     tenants uploading scene code, which is a capability edge if the studio is
     ever offered to tenants. That case is already a D7 revisit trigger, and
     this spike makes the swap a tested one.
   - Remotion's in-process React player can take a trace at view time. The
     HyperFrames player needs a composition built beforehand (D7 finding 1).
     For qontinui-web, which only embeds published scenes (D6), that is
     neutral.
2. **Scalability: HyperFrames was 3–5× faster on this host.** Throughput is
   not a constraint for the planned output (a weekly pulse plus a handful of
   clips), and the comparison was not tuned. This is not decisive.
3. **Robustness: Remotion is clearly ahead today, and this decides it.**
   - **Remotion:**
     - deterministic without a workaround;
     - goldens reproduced on this host, in a container and on CI;
     - versioned, documented API.
   - **HyperFrames:**
     - **API:** pre-1.0 with releases as often as hourly. Its `FrameAdapter`
       is "experimental v0". The documented way to drive React-like scene
       code (`registerFrameSource`) is 3 days old, so the adapter relies on
       the runtime tolerating a partial timeline.
     - **Determinism:** it needed an adapter-side workaround, and its own
       source documents SwiftShader compositor defects it is still working
       around.
     - **Reproducibility:** it gets it only from Docker. Upstream's image is
       not reproducible; ours is, but is not yet published.
4. **Clean code: about equal.** Each adapter is a few hundred lines behind the
   same seam. HyperFrames adds a page bundle and Docker plumbing; Remotion adds
   the licence workaround (peer dependencies, §7, a lazy chunk).

**What promoting it would require:**

- **Phase 4's licence step changes.** Without a Remotion adapter, qontinui-web
  needs no AGPL §7 additional permission. The MIT-studio and
  Remotion-as-peer-dependency arrangement and the Remotion line in NOTICE go
  away; HyperFrames' Apache-2.0 notice is carried instead. If a Remotion
  adapter is kept as a fallback, the §7 permission is still needed for any
  build that bundles it.
- **Phase 4's embed changes.**
  - `PlayerEmbedProps` becomes a published composition plus a poster.
  - Replay attestation moves to publish time.
  - The player's CDN runtime fallback is self-hosted or made impossible.
  - The poster is supplied at display size.
- **A published render image** pinned by digest, and the HyperFrames
  determinism test (`pnpm test:hyperframes`) as a CI job with
  `timeout-minutes`, telemetry off and `--network none`.
- **HyperFrames goldens.** Its frames differ from Remotion's (see parity), so
  the golden gate needs its own SHA-256 set.
- **A re-vet of D7**, as the scout addendum says. The decision, its licence
  reasoning and Phase 0's AGPL handling all change.

## Verification (this branch)

- `pnpm typecheck`, `pnpm lint`: clean.
- `pnpm lint:selftest`: 91/91 fixtures (12 new HyperFrames fixtures).
- `pnpm test`: 123 tests. They include 2 new CLI refusals, the graph test's
  HyperFrames control, and 11 Docker-free adapter refusals
  (`tests/unit/hyperframes-adapter.test.ts`):
  - `render` and `renderStills` each refuse a hand-written replay, scale 0.5,
    and an existing target;
  - `writeProject` refuses a hand-written replay;
  - empty stills return `[]`;
  - image references and bind-mount paths are validated.
- `pnpm test:graph`: 34 tests.
- `pnpm test:golden`: the existing Remotion suite, unchanged and green (51 passed, 9 skipped, 196 s). The
  HyperFrames file is skipped there unless `HYPERFRAMES=1`.
- `pnpm test:hyperframes`: 9/9 in 127 s, and 9/9 in 179 s, 181 s and 189 s after the reviews
  fixes (hardened container, explicit 4 workers against 1). With the remount
  removed, 7/9 fail.

## Reproducing

```sh
pnpm install && pnpm hyperframes:image
pnpm test:hyperframes
pnpm studio-scenes render s1-plan-lifecycle --trace traces/illustrative/s1-plan-lifecycle.json \
  --engine hyperframes --format png --aspect 16:9 --out out/hf-16x9
```
