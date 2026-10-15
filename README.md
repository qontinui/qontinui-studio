# qontinui-studio

The asset pipeline for products shipped by the Qontinui software factory: briefs
in, slot-cropped, labelled, provenance-carrying images out, published by
automated gates.

It also holds the launch kit, which turns a shipped product into a launch bundle
built from the factory's build record.

Plan: `qontinui-dev-notes/plans/2026-10-09-factory-built-product-portfolio-and-launch-kit.md`
(Phase 0 provider inventory, Phase 2 asset pipeline, Phase 3 launch kit).

## Separation: the studio is a client of the factory

The factory (coord, qontinui-web, the runner) holds the digital twin of how
software gets built. This repo is **not** part of it. It is a product the
factory builds, and it talks to the factory only over the doors any tenant
uses:

- it posts operator notices to coord's findings door;
- it reads the factory's build-record export (plan Phase 1) for the launch kit;
- it registers coord gates that run the launch kit when a product ships.

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
| **a5ec7670** every generated file is marked and provenanced (plan 2026-10-13 Phase 11) | Every image the studio generates or derives (raw download, every PNG/WebP/AVIF/JPEG variant) carries the IPTC `DigitalSourceType` = `trainedAlgorithmicMedia` XMP marker and a `<file>.provenance.json` sidecar: provider, adapter, model, terms as reviewed, the prompt sent, negative prompt, references (with sha256), seed when known, recipe, `created_at`, and — for the local provider — the facade's `qontinui_provenance` verbatim (SPDX licence, recipe hash, weight sha256s, seed). An incomplete sidecar is never written; D5 refuses a generated file without the marker or a matching sidecar. A published file is a byte-identical copy of its variant, and its sidecar travels in the site manifest per format. C2PA is not written: it needs a signing credential, which is a billing decision not taken. | `marker.py`, `provenance.py`, `pipeline.py` (`ingest`), `gates.py` (`d5_provenance`), `publish.py` |
| **D6** AI imagery is never presented as the real thing | Refuses AI imagery in any `product_screenshot` slot, and in `food`/`premises` slots on a `real_business` site. Demo sites must carry an illustrative label. The label is written to the site's `studio-manifest.json` for the site build to render. | `gates.py` (`d6_not_the_real_thing`), `publish.py` |
| **D7** automated gates decide; the operator is notified, not asked | A batch publishes only when every gate passes, including UI Bridge page health on a preview of the batch, which runs before anything goes live. Page health is **required, with no waiver**. The operator gets a notice carrying a one-step `studio --root <abs path> unpublish --batch <id>`. The review page is not an approval queue. | `publish.py`, `notify.py`, `review.py` |
| **D3** public redaction is explicit | The launch kit runs qontinui-web's own validator on every build record. It is ported in `launch/allowlist.py`, and a parity test runs it over a shared fixture corpus. The validator checks key for key, per-slot value types and the `<path>: <reason_code>` grammar for unknowns. It also scans every string for private repo names, UUIDs and emails, after Unicode normalisation that defeats lookalike and invisible characters. It enforces the caps: strings of at most 512 characters (decomposed too, with at most 2× NFKD expansion) and documents of at most 256 KiB. Any repo the product declares but the record left out (at most 50 such repos) must not be named anywhere in the record. A record must belong to the product: matching slugs, and only repos the product declares. Only a versioned public snapshot can back a published launch. | `launch/allowlist.py`, `launch/record.py`, `launch/publish.py` |
| **D1** the record is the credibility asset | The "How this was built" page is rendered only from the record, and every value names its document field. A null is shown as `unknown — <reason>`, never as 0. List counts are judged from coord's reason codes: `door_unavailable` (or any code other than `not_established`) makes a count unknown, and `not_established` on a non-empty list makes it a lower bound. A bundle without a record does not publish. | `launch/facts.py`, `launch/bundle.py` |

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

`providers.yaml` covers the fleet's own GPU (`local-gpu`) and ten third-party
providers: Gemini, OpenAI, Bing Image Creator / Designer, Leonardo, Ideogram,
Firefly, Runway, Pika, Luma and Kling. Each third-party row was reviewed on
2026-10-09, and every yes/no cites a source URL. Run `studio providers list` to
see the verdicts. In summary:

- **`local-gpu` is the default provider** (plan
  `2026-10-13-image-generation-and-visual-judgment`, D5) and the first preference
  of the shipped briefs. It is a self-hosted, OpenAI-compatible image facade
  behind llama-swap (`http://127.0.0.1:8100/v1` on spaceship;
  `STUDIO_LOCAL_GPU_BASE_URL` overrides it per machine) driven by the
  `openai_compatible_images` adapter. Its `capacity: unmetered` means it spends
  no quota: the host bounds itself with typed `busy`/`deferred` refusals. The
  loader allows `unmetered` only on that adapter kind, and the scheduler refuses
  it unless the base URL is on the local network (loopback, private, tailnet), so
  a public host can never be scheduled as free. Until the facade is deployed
  (plan Phase 10), a request is refused at connect and reported as a retryable
  `busy`.

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
poetry run studio library list [--json] [--batch ID] [--status S]... [--origin O]
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
`3` generation deferred (quota, a host's `busy`/`deferred`, an `unknown` being
retried) or failed, `4` no build record yet, `5` a trigger was registered but its
read-back did not confirm it, `6` image generation refused before any provider
call (`gateway_has_no_image_route`).

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

A **rejection** binds the launch kit: it leaves a rejected asset out of a new
bundle and says so in an operator note (decoration is optional), and its
publish refuses a bundle that still carries one (gate `library_approval`), for
example one assembled before the rejection. `candidate` assets stay usable. A
rejection withdraws nothing already live: unpublish the batch or launch for
that. External consumers are expected to filter with `--status`.
`studio library list --status approved|candidate|rejected` (repeatable) and
`--origin generated|imported` narrow the listing; with `--json` that is what a
consumer project reads to pick assets.

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
| `STUDIO_LOCAL_GPU_BASE_URL` | the `local-gpu` row's facade address on this machine (`base_url_env`); unset means the row's loopback `base_url` |
| `QONTINUI_RUNNER_URL` | where `brief run`, `jobs run`, `launch build` and `launch run` read the runner's model-gateway state (`GET /model-gateway`; default `http://127.0.0.1:9876`) |
| `STUDIO_WEB_BEARER` or `STUDIO_WEB_BEARER_FILE` | the launch kit's authed build-record door (`record.door: authed`) |
| `STUDIO_COORD_BEARER` or `STUDIO_COORD_BEARER_FILE` | `studio launch register-trigger`, and operator notices posted to `https://coord.qontinui.io/coord/agent-findings`. This needs a device/agent JWT. Without one, or when coord refuses, the notice is written to `.studio/notices/` and says why. Notice files are timestamped and never overwritten. |

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

**Typed job states** (plan 2026-10-13 Phase 11). Every provider-side outcome is
recorded in `job_attempts` as `done | failed | unknown | deferred | busy`, and
`brief run` / `jobs run` print each waiting job's last one.

- `busy` (the host's card is taken, or nothing is listening — the job provably
  never started) and `deferred` (a `priority: bulk` brief outside the host's bulk
  window) requeue the job at the host's `retry_after_s` **without spending a quota
  unit or an attempt**. A `deferred`, or a `busy` answered to a bulk job, never
  moves the job to another provider, so bulk work cannot spill onto a paid
  adapter.
- `unknown` — the transport failed after the request was sent, the answer was
  truncated, or the job id was lost — is never collapsed into `failed` or `done`.
  It is recorded as `unknown` and the job is retried as a counted attempt.
- An interactive job whose provider answered `busy` or `unknown` may fall back to
  the next preferred provider, but only one with capacity now: a cloud adapter
  only within its own `daily_quota`, which is unset today (no cloud work).

A brief's `negative_prompt` goes to a wire that has the field (the local facade)
as its own field; a wire without one (Gemini, OpenAI) gets it folded in as
`Avoid: …` text, and the job records exactly the text sent. A brief may also set
`seed`, `recipe` (a recipe id on the host) and `priority` (`interactive` |
`bulk`).

**Gateway tenants.** Before generating, the CLI reads the runner's model-gateway
state. No runner, or a runner build with no gateway read, means no declared
gateway. A declared gateway with an images route carries every OpenAI-images-wire
adapter; a declared gateway without one, or a runner that cannot say, refuses
every image call with `gateway_has_no_image_route` (exit 6) and makes no vendor or
local call (decision record
`gateway-tenants-route-every-model-call-through-the-gateway`).

### Layout

- `providers.yaml`: the terms inventory (Phase 0). Each row declares the
  provider's own `domains`, and every source URL must sit under one of them.
  There is at most one row per API adapter kind, and no domain, name or key
  env var may be shared between rows. A row may set `base_url` (and
  `base_url_env` to override it per machine) and `capacity`
  (`daily_quota` | `unmetered`).
- `sites/*.yaml`: publication targets. Each declares its kind (`demo` /
  `real_business` / `product`), whether it is `commercial`, `publish_dir`,
  the four D7 preview fields, and per-slot size, category, formats, focal point and
  optional background removal.
- `products/*.yaml`: launch-kit products (slug, repos, record door, capture
  script, brief and assets site, publish target), plus their capture scripts.
- `briefs/*.yaml`: the asset id, purpose, targets (`{site, slot}` or
  `{campaign}`), aspect ratio, prompt, style guide, reference images, provider
  preference and number of variants.
- `schemas/import-manifest.schema.json`: the `library import` manifest contract.
- `.studio/` (gitignored): the SQLite library and scheduler state, raw downloads
  and imports, variants, notices and the review page.

## Launch kit (plan Phase 3)

`studio launch run products/<slug>.yaml` takes a product from shipped to
launched in one step:

1. **Build record.** The kit fetches the public snapshot
   `GET https://api.qontinui.io/api/v1/public/build-records/<slug>`, or, with
   `record.door: authed`, the authed proxy `GET /api/v1/build-records/<slug>`
   using a bearer from `STUDIO_WEB_BEARER` / `STUDIO_WEB_BEARER_FILE`. The bearer
   is sent only over https to an allowlisted host (`api.qontinui.io`); any other
   door is refused as `refused_door`. The kit verifies the snapshot's
   `content_sha256` and runs the D3 validator. It also requires the record to be
   this product's: `public_slug` equals `document.product.slug` equals the
   product slug, and the document's repos are a subset of the product's.
   Only a public snapshot with a version can be published. An authed document or
   a `--record-file` builds a preview bundle only. The `d1_record` gate refuses
   to publish it, and no non-public URL is written into its files.
   A 404, an unreachable route or a refused credential is a typed
   `NoBuildRecord`, not a crash and not an empty record. The bundle still
   assembles, says there is no record, shows no numbers, and does not publish
   (exit `4`).
2. **Capture.** A declarative action script (`navigate` / `click` / `fill` /
   `wait` / `screenshot` steps in YAML) is run against the running product. The
   kit can also serve a static directory on `127.0.0.1` for the capture.
   Playwright is the headless browser, which is its one sanctioned use here. It
   produces real screenshots and records a walkthrough video. A `navigate`
   target is resolved against the product's URL and must stay on its origin, so
   `file:`, `javascript:` and other sites are refused. Requests to other
   origins are blocked, and a screenshot is refused if the page has left the
   product. When the kit captures the studio's own UI, the review page, it
   builds a public variant with no absolute operator paths. Its images are
   copied into the served directory, so every image link resolves under the
   capture server. When recording is
   off or fails, ffmpeg stitches the screenshots instead. Playwright is the
   optional `launch` extra; without it the kit raises `PlaywrightUnavailable`
   and assembles without a capture.
3. **Decoration (optional).** The product's brief runs through the Phase 2 path
   (brief, scheduler, library, D5/D6/D7 gates) onto its `assets_site`. The kit
   reads only what those gates published, minus any asset rejected in the
   library, which is skipped with a note. None of it is required; every failure
   becomes a note in the bundle.
4. **Bundle.** The bundle is a static directory with these files:
   - `index.html`: the landing page, with OG meta;
   - `how-it-was-built.html`: rendered from the record only;
   - `build-record.json`;
   - `social/{og,x,linkedin,square}.png`: 1200×630, 1200×675, 1200×627 and 1080×1080;
   - `posts/{x,bluesky,mastodon,linkedin}.txt`: known facts only, plus the record link;
   - `video/launch.mp4` and `film.json`: about 60 s;
   - `launch-manifest.json`: the SHA-256 of every file.

   Film frames and segments are rendered outside the bundle in temporary
   directories and removed whatever happens. A bundle can never list a
   dot-prefixed path: assembly refuses one, and so does the integrity gate.
   Each image's D7 group is a readable slug of its path plus a hash of the
   exact path, and a collision is refused when the bundle is assembled.

   `--out` is refused when it:
   - equals or overlaps any product's or site's publish or preview dir, or a
     registered launch's publish dir or bundle;
   - holds `.studio.lock` or a `.studio-journal-*` file;
   - holds a bundle that a launch row owns (a bundle is never rebuilt in place);
   - holds anything other than an unregistered `launch-bundle/1` bundle.

   An empty directory is accepted. The bundle is built in a temporary sibling
   directory and renamed into place.

   The film runs in this order: a title, the walkthrough, build-record summary
   slides, a replay slot, AI b-roll labelled in the frame, and a closing card.
   The **replay slot** is pluggable (`ReplaySegment`, or `--replay-clip FILE`).
   It holds the sibling plan's animated build-record scene. Until that scene's
   renderer exists, the slot is omitted and `film.json` says why.
5. **Gates and publication (D7).** The pre-publication gates are:
   - `d1_record`, `d3_allowlist` and `integrity`;
   - `d1_current`: the public door is re-read at publish time, and it must still
     serve the very snapshot (version and `content_sha256`) the bundle was built
     from. A retracted or newer record refuses;
   - `d1_tenant`: the snapshot must be OWNED by the operator's tenant. Public
     slugs belong to whichever tenant published first, while product slugs are
     per-tenant, so another tenant can own the public snapshot of a slug the
     operator also opted in, and a product listing proves nothing. The proof
     comes from the authed `GET /api/v1/build-records/{slug}/publication`: it
     must answer `owned_by_caller_tenant: true`, not `unpublished`, with the
     bundle's own version and `content_sha256`. The answer is checked exactly
     as qontinui-web serves it: the keys `public_slug`,
     `owned_by_caller_tenant`, `unpublished`, `version` and `content_sha256`,
     no more and no fewer. `public_slug` must be this slug, and a not-owned
     answer must carry no state. When that route answers 404 or
     no web bearer is configured, the product definition must pin
     `record.expected_sha256` and/or `record.expected_version`. Otherwise
     publication is refused;
   - `public_text`: no text the kit composes from local inputs may carry an
     absolute local path, the operator's home directory, this machine's
     hostname, or a machine-internal host (`localhost`, loopback, `*.local`,
     `file://`). That covers the manifest, posts, `film.json`, captions and the
     page templates. Pages are judged by position, never by matching content:
     - the gate re-runs the one page renderer on the bundle's own inputs, which
       proves the pages are exactly that render;
     - it renders them again with every record string replaced by a
       placeholder, and scans that render.
     
     So a PR titled "bind to 127.0.0.1" publishes, while a record string can
     never hide a leak in kit-composed text. For example, a homepage of
     `http://qontinui-studio.internal:8080/` is refused even though the slug
     is also a record string. The manifest, posts and `film.json` are scanned
     as written, and `build-record.json` is left to the D3 validator.
     Assembly notes and capture URLs are operator-only. They are kept in the
     launch row and the notice, never in the manifest;
   - `d5_provenance` and `d5_licence`, run per decorative image;
   - `library_approval`, run per decorative image: refuses an asset rejected in
     the library after the bundle was assembled (re-run the kit to drop it);
   - `d6`;
   - `footer`, which checks that every page links "Built by the Qontinui software
     factory" to the public build record;
   - `already_live`, which refuses a launch that is already the live publication;
   - `foreign_file`, which refuses when any file in the publish directory is not
     a file the live launch placed, still holding the bytes it placed. A hand
     edit or someone else's page is never overwritten. The studio's
     `.studio.lock` and `.studio-journal-*` files are ignored.

   D7 follows the asset pipeline's preview-first order and its UI Bridge
   binding, with no waiver. The target declares all four of `ui_bridge_url`,
   `preview_url`, `preview_dir` and `preview_manifest_url`, or none of them.
   None refuses as `not_configured`. A `#fragment` in any of the three URLs is
   refused, because D7 binds through the real query string. The bridge URL is judged by the asset
   pipeline's `forbidden_bridge_reason`. On ports 9875 and 9876 (the
   supervisor's and runner's own UIs) it is refused if the host, as httpx will
   see it and resolve it, is local, or if the host doesn't resolve. The checks
   run in this order:

   1. The bundle is written to `publish.preview_dir`.
   2. `d7_preview`: the preview serves exactly this bundle's
      `launch-manifest.json`, checked by sha256.
   3. `d7_navigate`: the preview's UI Bridge is driven, with a hard document
      load, to `preview_url?studio_launch=<id>&studio_attempt=<uuid4>`. A fresh
      attempt token is used every run. The kit then polls the snapshot until
      the page reports exactly that URL, compared browser-normalised and with
      the preview's origin, retrying bridge errors up to a deadline.
   4. `d7_page_health`: UI Bridge page health must say OK or WARNING.
   5. `d7_images`: the kit keeps polling, within the same deadline, until every
      image on the page has settled (loaded or failed), and the page must not
      move meanwhile. This check covers every image in the bundle.
      - The landing page renders each image with `loading='eager'`, its
        `width`/`height`, and a unique `data-media-id`, in the form
        `studio-img-launch-<path>`. D7 finds each image by that id and never by
        file name. A bridge that reports no media at all is refused, because it
        needs media discovery. Every URL naming a bundle image carries
        `?sha=<sha256>`, and the manifest records each image's sha256.
      - An image on the landing page must be shown from exactly
        `urljoin(preview_manifest_url, <path>)`, compared by scheme, host, port
        and full path, so it is served beside the launch manifest. It must
        carry its `?sha=` and load. The bytes at the exact URL the element
        shows are fetched and hashed.
      - An element with no `src`, a lazy image, or one still pending at the
        deadline is refused. `preview_manifest_url` must be on the same origin
        as `preview_url`.
      - An image the page doesn't show (the social cards) is fetched from the
        preview, relative to `preview_manifest_url`, and its sha256 compared.

   Nothing live changes until every check passes.

   The preview dir has the same limits as a site's:
   - it must be outside the studio root and must not contain it;
   - it must be neither `publish_dir`, inside it, nor a parent of it;
   - neither the preview dir nor the publish dir may overlap any site's publish
     or preview dir;
   - the kit only deletes files listed in the preview manifest it wrote itself.

   Publication also holds a lock on the bundle directory from the first gate to
   the commit. Before each file is placed, its staged bytes must hash to the
   manifest entry D7 verified. The manifest itself must be the bytes the preview
   served. A bundle edited mid-publish is refused, and nothing goes live.

   Publish and unpublish both hold the OS locks on the publish dir and the
   preview dir (`.studio.lock`, `locks.site_locks`) until the notice is sent.
   Both are journalled by `LaunchJournal` (`.studio-journal-<launch>.json`),
   which follows the pipeline's protocol with bundle-relative paths:
   - every original is re-checked by sha256 right before it moves, so a file
     that changed since the check is never moved or overwritten;
   - a backup that isn't the kit's own bytes is kept;
   - a journal is validated before recovery acts on it. Its paths must stay
     inside the publish dir, and its launch id must match the file name and
     belong to that directory.

   The DB transaction is the commit point, and the gate results are written
   inside it. After the commit, a problem with cleanup or the notice is
   reported as a warning, never as a refusal.

   Recovery runs at the start of every publish or unpublish of that directory,
   or for all of them with `studio launch recover`. It replays a leftover
   journal forward if the change committed and backward otherwise, and notifies
   the operator either way (`publish-rolled-back` / `unpublish-rolled-back`). It
   also refuses launches stuck in `publishing`, and re-sends the published
   notice for a live launch whose `notified_at` is unset.

   On success, the previous launch is marked superseded. The operator gets a
   notice with a shell-quoted `studio --root <abs path> launch unpublish --launch <id>`.
   Only the live publication can be unpublished. Unpublish never restores an
   earlier launch on its own. To restore one, run
   `studio launch publish --launch <id>`, which re-runs every gate, D7 on a
   preview included.

```bash
poetry install -E launch && poetry run playwright install chromium
poetry run studio launch record qontinui-studio            # fetch + validate; exit 4 = no record yet
poetry run studio launch build products/qontinui-studio.yaml [--record-file F] [--no-video] [--no-capture] [--no-generate] [--replay-clip F]
poetry run studio launch run products/qontinui-studio.yaml  # build + gated publish
poetry run studio launch publish --launch ID
poetry run studio launch unpublish --launch ID              # one step
poetry run studio launch list
poetry run studio launch recover                            # replay interrupted publishes/unpublishes
poetry run studio launch register-trigger --work-unit-slug <plan-slug> --product qontinui-studio
```

### Trigger (no poller)

`register-trigger` first resolves the work unit read-only through the agent
read door, `GET /coord/agent-work-units/<slug>`. An unknown slug is refused and
nothing is written: the trigger never creates a work unit. The slug is
URL-quoted in every path, and the coord bearer is sent only over https to the
allowlisted coord host (`coord.qontinui.io`). Any other `--coord-base` is
refused before a request is made. It then registers
this gate on
`POST https://coord.qontinui.io/coord/work-units/<slug>/register-gate`:

```json
{"kind": "unit_status", "work_unit_id": "<uuid>", "status": "shipped"}
```

The gate carries a `run_skill` continuation for the `launch-kit` skill
(`.claude/skills/launch-kit/SKILL.md`). The command uses the bearer from
`STUDIO_COORD_BEARER` / `_FILE`. It prints the `gate_id` and reads the gate back
through `GET /coord/agent-gates?work_unit_id=`. It exits `0` only when that
read-back lists the gate, and `5` when the read-back is absent or unreadable.

### Self-launch: what is still owed

`products/qontinui-studio.yaml` launches the studio itself. Its walkthrough is
the studio's own UI, the review page. `tests/test_launch_end_to_end.py` runs the
whole kit offline, on a fixture build record and a local test site. That run
includes the shipped product definition, Playwright capture and a short ffmpeg
film. Two steps remain before the **live** self-launch:

- Phase 1 must be deployed. The public build-record route answers 404 today,
  which the kit reports as "no build record yet".
- The operator must decide which public org/domain hosts build records and the
  bundle. That decision sets `record.public_url`, `publish.public_url` and the
  four D7 preview fields: `ui_bridge_url`, `preview_url`, `preview_dir` and
  `preview_manifest_url`. Until those are declared, `studio launch run`
  assembles the self-launch and then refuses to publish it
  (`d7_page_health not_configured`, exit 2). This is deliberate: D7 is never
  waived. For the same reason, `sites/qontinui-studio-launch.yaml` refuses
  decoration until it has a preview.

## Development

```bash
poetry run ruff check . && poetry run ruff format --check . && poetry run mypy && poetry run pytest
```

Tests never call a provider. The API adapters run against HTTP mocked with
`respx`; the local adapter runs against a fake OpenAI-compatible image host on a
real loopback socket (`tests/imagehost.py`), so a transport drop is a real
dropped connection. The end-to-end acceptance test (`tests/test_end_to_end.py`) uses the
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
