---
name: launch-kit
description: Run the qontinui-studio launch kit for one portfolio product — fetch its build record, capture the running product, assemble the launch bundle and publish it through the automated D7 gates. A product that publishes to the build.qontinui.io hub (publish_dir under ${STUDIO_HUB_CLONE}) is published by `hub publish <slug>` instead, which runs the kit and opens the hub PR. This is what the coord unit_status=shipped gate registered by `studio launch register-trigger` runs when the product's work unit ships. Argument is the product slug (products/<slug>.yaml).
---

# launch-kit

Invoked by a coord gate continuation as `run /launch-kit <product-slug>`, or by
hand. The argument is the slug of a product definition at
`products/<product-slug>.yaml` in the qontinui-studio checkout.

## Which arm

Read `publish.publish_dir` in `products/<product-slug>.yaml` as written (the raw
text, before any expansion):

```bash
grep -E '^\s*publish_dir:' products/<product-slug>.yaml
```

- It starts with `${STUDIO_HUB_CLONE}`: the bundle goes live on
  build.qontinui.io through a hub PR. Take **the hub arm** below.
- Anything else: take **the studio arm** (Steps).

## The hub arm

For a hub product, **never run `studio launch run` (or `studio launch publish`)
directly.** It would write the bundle into the hub clone with no branch and no
PR, and leave exactly the dirty tree `hub publish` refuses to reset. The hub
command wraps the kit: it starts the 127.0.0.1:4330 preview, runs
`studio --root ${STUDIO_ROOT} launch run <resolved yaml>` inside it with every
D7 gate, then branches, commits only `public/<slug>/` (and the exhibit line),
pushes, opens the PR, reads it back by head sha, and posts one coord finding
(topic `studio-publication`).

It runs only on the studio host, which holds the hub clone, the studio DB and
the web credential. Run it from the workspace root's hub clone:

```bash
node "$QONTINUI_WORKSPACE_ROOT/.studio/hub-publish/bin/hub.mjs" publish <product-slug>
```

`$QONTINUI_WORKSPACE_ROOT` is the directory holding the repo checkouts. When it
is unset but `$STUDIO_HUB_CLONE` is exported, that variable already names the
same clone (`<workspace-root>/.studio/hub-publish`), so run
`node "$STUDIO_HUB_CLONE/bin/hub.mjs" publish <product-slug>`. If neither
resolves to an existing `bin/hub.mjs`, this machine is not the studio host.
Report that as BLOCKED with the path you looked at; do not fall back to the
studio arm.

Report what it printed, as it is:

- **The PR** (`<owner/repo>#<n>` and its URL) and **the hub's finding** (its id).
  The page goes live only **when coord lands that PR**, so report the launch as
  "PR open, live on land", never as live.
- A non-zero exit: report the exit code and the hub's message verbatim. If it
  names a step for the operator or another command (for example
  `hub record-publish <slug>` when the public record route answers 404, or an
  open hub PR for the slug), relay that step. Do not retry around a refusal.

The one-step retraction for a hub product is `hub unpublish <slug>` (the
product's `publish.unpublish_command`, which the studio's notice prints), not
`studio launch unpublish`.

## Steps (the studio arm)

1. Work in the qontinui-studio checkout. Install with the launch extra if it
   is not installed yet:
   `poetry install -E launch && poetry run playwright install chromium`.
2. Run the kit, which builds the bundle and publishes it only if every gate
   passes:

   ```bash
   poetry run studio launch run products/<product-slug>.yaml
   ```

3. Read the exit code and report it as it is:
   - `0`: published. The operator notice carries the URLs and the one-step
     unpublish: the product's `publish.unpublish_command` when it sets one,
     else `studio launch unpublish --launch <id>`. Relay the launch id and that
     command.
   - `2`: a gate refused publication. The output names each failing gate. Fix a
     cause that is mechanical (for example a capture script step that no
     longer matches the product) and run again. Otherwise report the gate and
     its detail.
   - `4`: no build record yet. The factory's public route answered 404 or was
     unreachable, so nothing was published. This is not a failure of the kit. The
     record has to be published first (qontinui-web
     `POST /api/v1/build-records/<slug>/publish`, which is the tenant's opt-in).
     Report it as UNKNOWN or waiting, never as success.
   - `1`: a config error. Report the message.

Never publish a bundle by copying files by hand. Publication is what the gates
decide (D7), and only `studio launch run` / `studio launch publish` record it
with its one-step unpublish (for a hub product, only through `hub publish`).
