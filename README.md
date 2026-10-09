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
| **D7** automated gates decide; the operator is notified, not asked | A batch publishes only when every gate passes, then a UI Bridge page-health check runs. A CRITICAL or unreachable result rolls the batch back. The operator gets a notice with a one-step `studio unpublish --batch <id>`. The review page is not an approval queue. | `publish.py`, `notify.py`, `review.py` |

D3, the build-record allowlist, gates the factory's export (Phase 1). It has no
subject in this repo.

A site that declares no `page_health_url` reports the page-health gate as
`not_configured`, which **refuses** publication. The one exception is a demo
site that sets `page_health: waived` (allowed on demo sites only, e.g. a template
with no running preview yet). Every operator notice for such a site prints the
waiver.

Publication is transactional. New files are staged under hidden names, the
slot's current files are moved aside, and the new ones are renamed into place.
Manifests are written via a temp file and `os.replace`. The page-health probe
then checks the new content. If it fails, or any step raises, the previous files,
manifest and publication rows are put back. Unpublishing the newest batch on a
slot restores the publication it replaced. A batch publishes only when every
`{site, slot}` its brief targets has a variant.

## Provider inventory: the honest D4 outcome

`providers.yaml` covers ten providers: Gemini, OpenAI, Bing Image Creator /
Designer, Leonardo, Ideogram, Firefly, Runway, Pika, Luma and Kling. Each was
reviewed on 2026-10-09, and every yes/no cites a source URL. Run
`studio providers list` to see the verdicts. In summary:

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
transport) are retried with exponential backoff. After repeated failures, or a
permanent failure, the job moves to the next preferred provider. All of this
state is kept in `.studio/studio.db`.

### Layout

- `providers.yaml`: the terms inventory (Phase 0).
- `sites/*.yaml`: publication targets. Each declares its kind (`demo` /
  `real_business` / `product`), whether it is `commercial`, `publish_dir`,
  `page_health_url`, and per-slot size, category, formats, focal point and
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
