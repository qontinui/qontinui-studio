---
name: launch-kit
description: Run the qontinui-studio launch kit for one portfolio product — fetch its build record, capture the running product, assemble the launch bundle and publish it through the automated D7 gates. This is what the coord unit_status=shipped gate registered by `studio launch register-trigger` runs when the product's work unit ships. Argument is the product slug (products/<slug>.yaml).
---

# launch-kit

Invoked by a coord gate continuation as `run /launch-kit <product-slug>`, or by
hand. The argument is the slug of a product definition at
`products/<product-slug>.yaml` in the qontinui-studio checkout.

## Steps

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
     `studio launch unpublish --launch <id>`. Relay the launch id and that command.
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
with its one-step unpublish.
