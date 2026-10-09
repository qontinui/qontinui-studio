# qontinui-studio

The asset pipeline for products shipped by the Qontinui software factory: briefs
in, slot-cropped, labelled, provenance-carrying images out, published by
automated gates.

Plan: `qontinui-dev-notes/plans/2026-10-09-factory-built-product-portfolio-and-launch-kit.md`
(Phase 0 provider inventory, Phase 2 asset pipeline).

## Separation: the studio is a client of the factory

The factory (coord, qontinui-web, the runner) holds the digital twin of how
software gets built. This repo is **not** part of it. It is a product the
factory builds, and it talks to the factory only over the doors any tenant
uses:

- it posts operator notices to coord's findings door;
- it will read the factory's build-record export (plan Phase 1) for the launch kit.

Third-party generator dependencies (API keys, provider terms) live here, so
marketing churn never touches merge-critical factory code.

## Design rules enforced as code

| Rule | What it means here | Where |
|---|---|---|
| **D4** API first, GUI automation only where the terms allow it, one account per provider | An adapter is built only for a provider whose `providers.yaml` row has `tos_reviewed` set and `automation_allowed: "yes"`. `unknown` keeps it off. A duplicate provider row is a load error. | `providers.py` (`adapter_gate`), `adapters/__init__.py` |
| **D5** every asset carries provenance and licence | Each library row stores provider, model, prompt, brief id, date and SHA-256. It also snapshots the licence terms as reviewed, and the watermark. `ai_generated` is pinned true by a SQL `CHECK`. An unreviewed licence is refused for every site; a non-commercial licence is refused for a commercial site. | `store.py`, `library.py`, `gates.py` (`d5_licence`, `d5_provenance`) |
| **D6** AI imagery is never presented as the real thing | Refuses AI imagery in any `product_screenshot` slot, and in `food`/`premises` slots on a `real_business` site. Demo sites must carry an illustrative label. The label is written to the site's `studio-manifest.json` for the site build to render. | `gates.py` (`d6_not_the_real_thing`), `publish.py` |
| **D7** automated gates decide; the operator is notified, not asked | A batch publishes only when every gate passes, including UI Bridge page health on a preview of the batch, which runs before anything goes live. Page health is **required, with no waiver**. The operator gets a notice carrying a one-step `studio --root <abs path> unpublish --batch <id>`. The review page is not an approval queue. | `publish.py`, `notify.py`, `review.py` |

D3, the build-record allowlist, gates the factory's export (Phase 1). It has no
subject in this repo.

A site that declares no UI Bridge preview reports the page-health gate as
`not_configured`, which **refuses** publication. There is no waiver (plan D7).
The two shipped template sites (`sites/cafe-template.yaml` and
`sites/barber-template.yaml`) declare no preview yet, because their repos are
plan Phase 4, so **publishing to them is refused until a preview is
configured**. The end-to-end acceptance test configures a fake preview server
and UI Bridge (injected `fetch` and `bridge`) for those sites. Every D7 check
runs for real against it, including image decoding from the preview dir.

`preview_dir` is rewritten on every publish, so it has these limits:
- It must be outside the studio root and must not contain it.
- It must not equal, contain or sit inside ANY site's `publish_dir`, or another
  site's `preview_dir`. Publish dirs must not overlap each other either.
- It is locked while it is written.
- The studio only ever deletes files listed in the preview manifest it wrote
  itself.

Publication is safe against failures, crashes and concurrent runs:

- **Exclusive.** Every publish and unpublish holds an OS file lock
  (`<publish_dir>/.studio.lock`) on each affected site, from the first check
  to the operator notice. A batch is driven by one process at a time; the
  batch row carries a lease. Each job step runs under a claim token, and a
  late result from a reclaimed claim is discarded.
- **Never clobbers.** If a slot path already holds bytes that are not a live
  studio publication of that slot, the whole batch is refused (`foreign_file`).
  This covers an owner-supplied photo or a hand edit. A batch that is already
  live is refused (`already_live`).
- **D7 runs on a preview of this batch, before anything goes live.** A site
  declares `ui_bridge_url`, `preview_url`, `preview_dir` and
  `preview_manifest_url`, all four or none, and none of them with a `#fragment`.
  A hash-routed preview URL would put D7's batch and attempt binding inside
  the fragment, where navigation is a same-document change.
  - `ui_bridge_url` is the `/control/*` prefix of the UI Bridge in the browser
    showing the preview. The preview page must run the UI Bridge React
    auto-registration provider with media discovery on. An injected/seeded
    bridge registers interactive elements only, so it reports no media and D7
    refuses it with that message. Ports 9875 and 9876 (the supervisor's and runner's own UIs) are
    refused on any host that is this machine. The host is judged exactly as
    httpx will request it, after IDNA normalisation (`127。0。0。1` becomes
    `127.0.0.1`), and then resolved. It is refused if it is local in any
    spelling (`127.0.0.2`, `127.1`, `0x7f000001`, `::ffff:127.0.0.1`,
    `0.0.0.0`), if ANY address it resolves to is loopback, unspecified or one
    of this machine's interfaces (`localtest.me`), or if it does not resolve at
    all, because UNKNOWN refuses.
  - The studio writes the live files, this batch's files and the merged
    manifest into `preview_dir`, and confirms the preview serves exactly that
    manifest (by sha256).
  - It drives the bridge to
    `preview_url?studio_batch=<batch>&studio_attempt=<fresh uuid>` with
    `POST /control/page/navigate` and `{"hard": true}`. Without `hard`, an
    app with a client-side router changes the URL but keeps the old
    document. The attempt token changes on every D7 run,
    including an unpublish restore's, so a tab still showing an earlier
    attempt's document never matches. It then polls `GET /control/snapshot` until
    `page.url` is that page, or a 30 s deadline passes. A bridge error while
    the tab re-registers is retried.
  - URLs are compared as a browser reports them: lowercase scheme and host,
    default port dropped, dot segments resolved, an empty path as `/`. The
    page's origin must be the preview's origin.
  - It runs page health (`POST /control/page-health`). Only `OK` or `WARNING`
    pass.
  - It keeps polling the snapshot, within the same deadline and without the
    page moving, until every changed slot's images have settled (loaded or
    failed). It then checks every changed slot. A browser renders a
    multi-format slot through `<picture>`, fetches ONE file, and the bridge
    reports that image's `currentSrc`.

    Slot elements are found by their `data-media-id`, never by file name:
    `studio-picture-<slot>` for the `<picture>` and `studio-img-<slot>` for the
    `<img>`. The kind comes before the slot name, so no two slots' ids can
    collide. UI Bridge's `generateMediaId` uses an explicit `data-media-id`
    first. Without one, it derives the id from the alt text, which is the same
    site-wide label on every slot, or from the sibling index. Those ids
    collide, and the registry silently drops duplicates. A missing id is
    refused by name. Then:
    - at least one image must show one of the slot's files from the preview's
      origin, carrying `?sha=<that file's sha256>` and loaded
      (`naturalWidth > 0`, `loadingState == "loaded"`);
    - every element and every `srcset` / `<source>` URL naming a slot file
      must carry that file's `?sha=`;
    - each format the page did not load is fetched from the preview server and
      its sha256 compared.
  - A slot marked `rendered: false` is meta-only, e.g. an `og:image`. It is
    verified by fetching each file from the preview server and comparing its
    sha256.
  - The preview drops exactly the superseded files the swap will move away,
    so D7 checks the state that actually goes live.

  Any failure, or any bridge error, refuses, and live content is never touched.
  The bridge has a navigate control (`/control/page/navigate` in
  `ui-bridge/packages/ui-bridge/src/server/handlers.ts`), so no separate
  headless browser is needed. A navigate is fire-and-forget, so the studio
  polls the snapshot until the page reports the target URL.
- **Journalled swap and unpublish.** Before any live file moves, a journal at
  `<publish_dir>/.studio-journal-<batch>.json` records every staged, placed and
  backup path, plus the manifest before and after. A single `BEGIN IMMEDIATE`
  transaction is the commit point. It marks the batch `published` and records
  the gate results. Any
  publish, unpublish or `jobs run` replays a leftover journal: forward if the
  database committed, backward otherwise. Undo removes only a live file that
  still holds the bytes this publish placed. A journal is acted on only after
  validation: its batch must exist, and every path it names must be a slot
  file, staging file or backup directly inside that site's own publish dir.
  Anything else is refused and the site is reported. Every original is
  re-checked right before it moves. A backup that does not hold a studio
  publication's bytes is never deleted. If cleanup fails after the commit, it is
  logged and left to recovery, and the batch is still reported as published. While a publish is in flight the
  batch is `publishing`; recovery turns an orphaned `publishing` into `refused`.
  Every rolled-back journal produces an operator notice. A published batch whose
  notice was never sent (`batches.notified_at` is null) is re-notified. Recovery
  runs per site: a site that cannot be recovered holds back only the batches
  that target it.
- **Complete or nothing.** A batch publishes only when every `{site, slot}` its
  brief targets has a variant.
- **Unpublish** works only on a live batch whose site configs are all loaded.
  It restores the publication the batch superseded only if that publication
  passes D5/D6 against the CURRENT site config and D7 on a preview of the
  result. Otherwise the slot is left empty and the notice says why. A restored
  slot carries the current site's label (the string D6 checked), in both the
  manifest and the publication row. It checks
  the stored variant's sha256 and never overwrites a live file holding bytes the
  studio did not place.
- **Nothing is stranded.** `studio jobs run` also publishes `generated` batches,
  unless the batch was created with `brief run --no-publish`.

### Site template guidance

A site template renders each studio slot from `studio-manifest.json`:

```html
<picture data-media-id="studio-picture-hero">
  <source type="image/avif" srcset="hero.avif?sha={{ slots.hero.sha256.avif }}">
  <img data-media-id="studio-img-hero"
       src="hero.webp?sha={{ slots.hero.sha256.webp }}" loading="eager"
       width="1600" height="900"
       alt="{{ slots.hero.label }}">
</picture>
```

- Every slot's `<picture>` and `<img>` carry `data-media-id="studio-picture-<slot>"`
  and `data-media-id="studio-img-<slot>"`. D7 finds slots only by these ids.
- The `<img>` has `width` and `height`, the slot's size from the site config.
  An image that has not loaded yet would otherwise be 0 px wide, and UI Bridge
  skips zero-size media as not visible.
- The preview page runs the UI Bridge React auto-registration provider with
  media discovery.

- Every file URL carries `?sha=<sha256>` from the manifest. It busts caches,
  and D7 checks it.
- Studio slot images must be `loading="eager"`. D7 refuses a `lazy` image,
  because a lazy image never loads in a preview nobody scrolls.
- Show the manifest's `label` beside the image (D6).
- Meta-only slots (an `og:image`) are declared `rendered: false`.

## Provider inventory: the honest D4 outcome

`providers.yaml` covers ten providers: Gemini, OpenAI, Bing Image Creator /
Designer, Leonardo, Ideogram, Firefly, Runway, Pika, Luma and Kling. Each was
reviewed on 2026-10-09, and every yes/no cites a source URL. Run
`studio providers list` to see the verdicts. In summary:

- **Every verdict cites the first-party URL that was checked.** For `unknown`
  that is the page that refused (e.g. HTTP 403) or rendered empty, with a note.
- **API adapters exist for Gemini (`google-genai`) and OpenAI Images.** Both pass
  D4 for API use. OpenAI's `commercial_use` is `unknown`: openai.com answered 403,
  and a third-party mirror is not accepted as a source (the loader rejects known
  mirror domains). So D5 refuses OpenAI assets until the first-party terms are read. Neither has a free API tier for image generation: Gemini's image
  models list "Not available" under the free tier. Their `daily_quota` is therefore
  unset, and **the scheduler assigns them nothing until the operator sets one**,
  because paying for API calls is the operator's decision.
- **No GUI-automation adapter is built.** Every provider without a usable free API
  either forbids bots or automation on its web UI or could not be confirmed:
  - forbids it: Bing, Ideogram, Firefly, Runway, Pika, Luma, Kling;
  - not confirmed: Leonardo (its terms answered HTTP 403), and the Gemini/AI Studio
    web UI.

  D4 permits GUI automation only where the terms allow it, so building one would
  break the rule this repo exists to keep. This is the outcome of the review, not
  a gap.
- **Operator input still owed:** the list of accounts the operator actually holds.
  The rows describe each provider's free tier, not those accounts. One account per
  provider applies.

## Using it

```bash
poetry install                 # add -E bg for background removal (rembg)
poetry run studio providers list
poetry run studio brief validate briefs/example-hero.yaml
poetry run studio brief run briefs/example-hero.yaml     # generate -> crop -> gate -> publish
poetry run studio jobs run                               # resume batches deferred by quota/backoff
poetry run studio library list [--json] [--batch ID]
poetry run studio publish --batch ID                     # re-run the gates and publish
poetry run studio unpublish --batch ID                   # one-step unpublish
poetry run studio review build                           # .studio/review/index.html
poetry run studio media trim IN OUT --start 1 --duration 4
poetry run studio media loop IN OUT --count 3 | --boomerang
```

Commands printed in notices and on the review page are shell-quoted for the
platform (`shlex` on POSIX, `list2cmdline` on Windows), so a root containing
spaces still pastes correctly.

Exit codes: `0` ok, `1` config error, `2` publication refused by a gate,
`3` generation deferred or failed.

### Configuring keys and quotas

| Variable | Used by |
|---|---|
| `GEMINI_API_KEY` | the `gemini` adapter. The env var name is set per row by `api_key_env`. |
| `OPENAI_API_KEY` | the `openai` adapter |
| `STUDIO_COORD_BEARER` or `STUDIO_COORD_BEARER_FILE` | operator notices posted to `https://coord.qontinui.io/coord/agent-findings`. This needs a device/agent JWT. Without one, or when coord refuses, the notice is written to `.studio/notices/` and says why. Notice files are timestamped and never overwritten. |

To let the scheduler use a provider, set `daily_quota` on its row. This is the
per-UTC-day cap. The scheduler works through a brief's `provider_preference` in
order and skips providers whose quota is spent. When every preferred provider is
spent, it defers the job to the next UTC midnight. Transient failures (429, 5xx,
transport) are retried with exponential backoff. A quota 429 (Gemini
`...PerDay...`, OpenAI `insufficient_quota`) is a different case. It marks the
provider spent until its quota reset, which is midnight in the row's
`quota_reset_tz` (Gemini: `America/Los_Angeles`), and the job waits; it is not
failed. A quota unit is spent and the job claimed in one `BEGIN IMMEDIATE`
transaction. Every lease is read from the clock at claim time, and the batch
lease is renewed on every scheduler iteration. A job is claimed atomically
(`submitting`) before its request leaves. That claim records the exact prompt,
the model and the provider's terms as reviewed, and ingest uses those values
rather than the current config. A claim whose process died is reclaimed after
`submit_stale_s`. A batch's hash covers the brief, its style guide and its
reference images, so editing any of them refuses to resume that batch. After repeated failures, or a
permanent failure, the job moves to the next preferred provider. All of this
state is kept in `.studio/studio.db`.

### Layout

- `providers.yaml`: the terms inventory (Phase 0). Each row declares the
  provider's own `domains`, and every source URL must sit under one of them.
  There is at most one row per API adapter kind, and no domain, name or key
  env var may be shared between rows.
- `sites/*.yaml`: publication targets. Each declares its kind (`demo` /
  `real_business` / `product`), whether it is `commercial`, `publish_dir`,
  the four D7 preview fields, and per-slot size, category, formats, focal point and
  optional background removal.
- `briefs/*.yaml`: the asset id, purpose, targets (`{site, slot}` or
  `{campaign}`), aspect ratio, prompt, style guide, reference images, provider
  preference and number of variants.
- `.studio/` (gitignored): the SQLite library and scheduler state, raw downloads,
  variants, notices and the review page.

## Development

```bash
poetry run ruff check . && poetry run ruff format --check . && poetry run mypy && poetry run pytest
```

Tests never call a provider. The API adapters run against HTTP mocked with
`respx`. The end-to-end acceptance test (`tests/test_end_to_end.py`) uses the
offline fake adapter: one brief produces exact-size crops for two template sites
with zero manual steps, and every library row carries full provenance.
