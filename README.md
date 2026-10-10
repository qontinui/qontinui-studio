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

## Tool and content: `--root` is the project

This repository is the **tool**. A *project* is whatever directory `--root`
points at (default: the current directory). It holds the project's content
config, tracked in the project's own repo:

- `providers.yaml`, `sites/*.yaml`, `briefs/*.yaml` (and style guides);

and its local state in `<root>/.studio/`, which is gitignored and never
committed: `studio.db`, raw downloads and imports, variants, notices and the
review page.

The `providers.yaml`, `sites/` and `briefs/` files in this repo are examples.
They are kept at the repo root for now and are not moved to `examples/`,
because the stacked launch-kit PR (studio#2) adds files beside them and its
README and tests reference those paths. Moving them belongs after that PR
lands.

## Design rules enforced as code

| Rule | What it means here | Where |
|---|---|---|
| **D4** API first, GUI automation only where the terms allow it, one account per provider | An adapter is built only for a provider whose `providers.yaml` row has `tos_reviewed` set and `automation_allowed: "yes"`. `unknown` keeps it off. A duplicate provider row is a load error. | `providers.py` (`adapter_gate`), `adapters/__init__.py` |
| **D5** every asset carries provenance and licence | Each library row stores provider, model, prompt, brief id (or, for an imported asset, account alias, reference images and terms URL), date and SHA-256. It also snapshots the licence terms as reviewed, and the watermark. `ai_generated` is pinned true by a SQL `CHECK`. An unreviewed licence is refused for every site; a non-commercial licence is refused for a commercial site. | `store.py`, `library.py`, `gates.py` (`d5_licence`, `d5_provenance`) |
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

- Slot files are served beside `studio-manifest.json`, and
  `preview_manifest_url` must be on the preview page's origin. D7 accepts an
  element only when its source is exactly `<manifest dir>/<file>`: same scheme,
  host, port and full path, not just the file name. D7 also fetches the exact
  URL the page shows, and every other format, and compares sha256.
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
poetry run studio library import INBOX_DIR               # images made elsewhere -> library (never published)
poetry run studio library approve UID --by ALIAS         # consumer-facing approval (D5: commercial_use yes only)
poetry run studio library reject UID --by ALIAS
poetry run studio library export --out library.yaml      # deterministic manifest of every asset
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

### The library: import, approval, export

**Import.** `studio --root <project> library import <inbox-dir>` brings in
images made outside the studio (for example by hand in a provider's web UI)
with the same D5 provenance a generated asset carries. The inbox holds the
image files and a `manifest.yaml` listing one entry per file. **The contract is
[`schemas/import-manifest.schema.json`](schemas/import-manifest.schema.json)**;
the studio validates manifests by hand against the same rules, and a test
cross-checks the schema file against the validator so the two cannot drift.

```yaml
- file: hero-1.png                  # relative to the inbox; no '..', no absolute path, no symlink
  asset_id: cafe-hero               # ^[a-z0-9][a-z0-9-]*$; several files may share one
  provider: midjourney
  model: v7
  account: studio-mj                # an ALIAS (^[A-Za-z0-9._-]+$), never an e-mail address
  prompt: An empty cafe interior in warm light
  reference_images:                 # an asset_id in this library, or {url, licence}
    - cafe-base-look
    - {url: https://example.com/ref.jpg, licence: CC-BY-4.0}
  generated_on: 2026-10-01          # ISO date or RFC 3339 timestamp
  licence_terms_url: https://example.com/terms
  tos_reviewed: 2026-10-02          # optional; commercial_use: yes needs it
  commercial_use: yes               # yes | no | unknown
  watermark: none                   # none | invisible | visible | unknown
  ai_generated: true                # must be true
  status: approved                  # candidate (default) | approved | rejected
  approved_by: josh                 # alias; approved/rejected need approved_by and approved_on
  approved_on: 2026-10-03
```

- The **whole** manifest is validated first. If any entry is invalid nothing is
  imported, and every problem is reported with its entry index.
- Width, height, SHA-256 and MIME type are **measured**: Pillow must decode the
  file as PNG, JPEG or WebP, whatever its extension says. A file over 50 MiB
  (`MAX_FILE_BYTES`) is refused before it is read. An image whose header
  declares more than 100 megapixels (`MAX_PIXELS`) is refused before its pixels
  are decoded, so a decompression bomb never expands. The manifest cannot
  supply them (unknown fields are refused).
- `commercial_use: yes` without `tos_reviewed` is recorded as `unknown`, and
  `approved`/`rejected` without both `approved_by` and `approved_on` is recorded
  as `candidate`. Both print a warning. `approved` with a recorded
  `commercial_use` other than `yes` is refused (D5).
- Each file is copied into `.studio/raw/imported/<sha[:2]>/<sha256>.<ext>`. The
  inbox is never referenced again and can be deleted.
- Re-importing the same bytes under the same `asset_id` is a no-op. The same
  name with different bytes is a new row. A re-import never rewrites recorded
  provenance: if the manifest's details changed, it warns and names the fields.
  Use `library approve|reject` to change approval.
- Each file is opened once. On POSIX the open starts from a descriptor on the
  inbox and walks every path component with `O_NOFOLLOW`, so a symlink, or a
  directory swapped for one mid-import, is refused. The file itself is opened
  `O_NONBLOCK`, so a FIFO cannot hang the import. The descriptor must be a
  regular file with one link and within the size cap. Its bytes are streamed
  once into a private staging file in
  `.studio/raw/imported/.staging/<run>/`, hashed while they are copied.
  Staging sits inside the raw store's directory, so moving a file into the
  store is a rename on one filesystem even when `.studio/raw` is on another
  disk. The size, SHA-256, format and dimensions all come from that one
  read, and the inbox is not touched again.
- On Windows, which has no `dir_fd`, the open falls back to a name-based
  walk. It refuses any component that is a symlink, a junction or another
  reparse point, opens the file by name, and then requires two things: the
  opened file is the one it walked to, and its resolved path lies inside the
  inbox. That refuses every link present when it looks. It is not race-free:
  a component swapped between the walk and the open is caught only by the
  resolve check after opening.
- `file` must be written plainly: no trailing separator and no empty (`//`)
  or `.` segments.
- The manifest is plain data. It is read the same way, capped at 1 MiB and
  1000 entries (`MAX_MANIFEST_BYTES`, `MAX_ENTRIES`). YAML anchors, aliases,
  merge keys and duplicate keys are refused. A manifest value appears in an
  error only as a truncated string, or else only as its type name. One import
  reads at most 2 GiB of files (`MAX_TOTAL_BYTES`).
- Two inboxes with the same directory name import into the same batch.
- Rows go in batch `import:<inbox basename>` (basename `^[a-z0-9][a-z0-9._-]*$`)
  with status `imported`. **An import is never published.** `publish`,
  `jobs run` and crash recovery all skip it, and a SQL `CHECK` refuses to move
  an `imported` batch to any other status.
- Staged files are renamed into the raw store inside the database write
  transaction. On a refusal or a rollback, this import's staging directory
  and any raw file it created are deleted. A failure to write is reported as
  a refusal that names the error code, never a local path.
- Staging directories older than 24 hours, left by a killed import, are
  removed by the next import while it holds the database write lock. The
  sweep removes only real directories (not symlinks) whose name is a 32-digit
  hex run id, and works through a descriptor on `.staging` itself. If
  `.staging` is a symlink, a junction or not a directory, imports refuse to
  stage there and the sweep is skipped with a warning. Two studio roots
  sharing one raw store do not share the database lock, so only the age
  check protects one root's staging from the other's sweep.

**Approval.** `studio library approve|reject <uid> --by <alias>` records the
decision, the reviewer alias and today's UTC date on the row. Approval is
refused unless the asset's licence is `commercial_use: yes` (D5, also a SQL
`CHECK`); rejection is always allowed. Approval is **library metadata for
consumers, not a publish gate**: generated batches still publish on the
automated D5/D6/D7 gates alone, with the operator notified rather than asked
(D7). Generated assets start as `candidate`.

**Export.** `studio library export --out <path>` writes every asset as YAML:
uid, asset_id, origin, batch, provider, model, account, prompt, reference images,
dates, SHA-256, size, MIME type, licence, watermark, `ai_generated`, approval,
and variants (id, site/slot/campaign, format, size, SHA-256). The SHA-256 is
the content-addressed pointer. The export contains no binaries and no local
paths. Keys are sorted and assets ordered by (asset_id, created_at, uid), so the
same library always exports to the same bytes.

**Schema versions.** `studio.db` carries `PRAGMA user_version`. Every database,
fresh or old, starts from the original (v0) tables and is migrated forward in
one transaction per version, so a database created before versioning is
upgraded in place the first time any command opens it.

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
- `schemas/import-manifest.schema.json`: the `library import` manifest contract.
- `.studio/` (gitignored): the SQLite library and scheduler state, raw downloads
  and imports, variants, notices and the review page.

## Development

```bash
poetry run ruff check . && poetry run ruff format --check . && poetry run mypy && poetry run pytest
```

Tests never call a provider. The API adapters run against HTTP mocked with
`respx`. The end-to-end acceptance test (`tests/test_end_to_end.py`) uses the
offline fake adapter: one brief produces exact-size crops for two template sites
with zero manual steps, and every library row carries full provenance.

## Licence

The whole repository is MIT-licensed (see `LICENSE`), including the `scenes/`
directory the sibling plan adds later. MIT, not AGPL, is deliberate: the planned
scene renderer will take Remotion (source-available) as a peer dependency, and
Remotion's terms are further restrictions that AGPL-3.0 §10 forbids. The
decision is recorded in plan
`2026-10-09-qontinui-marketing-studio-animated-process-explainers`, Phase 0
result ("AGPL handling", recorded by session 4409f0d3). It is tied to keeping a
Remotion adapter: that plan leaves open whether HyperFrames becomes the primary
adapter, and if no Remotion adapter remains the reason for MIT over AGPL lapses
(the choice itself stays valid). Remotion is never vendored here and stays under
its own licence.
