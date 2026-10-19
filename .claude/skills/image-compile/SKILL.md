---
name: image-compile
description: Compile a consumer repository's image slots with qontinui-studio — allocate a worktree of the repo, run `studio compile` against its frontend/image-slots specs (generate, choose, write derivatives and studio-manifest.json through the D5/D6/D7 gates), then commit only the publish dir and open the PR with the evidence studio wrote. Studio runs no git or gh; this session does. Argument is the compile target id (targets/<id>.yaml).
---

# image-compile

Plan `2026-10-13-image-generation-and-visual-judgment` Phase 19. The argument is
a compile target id: `targets/<id>.yaml` in the qontinui-studio project, naming
the repository (`owner/name`), where its slot specs live in a checkout
(`slots_dir`) and where derivatives land (`publish_dir`).

## Steps

1. Allocate a worktree of the target's repository — never a bare `git worktree
   add`, never the primary checkout:

   ```bash
   bash <workspace-root>/qontinui-claude-config/scripts/allocate-worktree.sh \
     --root <workspace-root> --repo <repo name> --intent "studio compile: <slot or reason>"
   ```

   W is the path it prints for that repo, B the branch.
2. From the qontinui-studio checkout (`poetry install` first if needed):

   ```bash
   poetry run studio compile --target <id> --slots <W>/<slots_dir> --json
   ```

   Add `--slot <slot id>` to compile only some slots, `--dry-run` to see the plan
   first. Set `STUDIO_IMAGEGEN_DIR` to a qontinui checkout's `docker/imagegen`
   when a slot names a LoRA or uses the fleet's own facade: without it those
   inputs read UNKNOWN and nothing is generated for them.
3. Act on the exit code, as it is:
   - `0`: every slot is compiled, fresh or selected.
   - `7`: work for you. `awaiting_alt_text`: open the slot's derivative files the
     report lists, write a factual alt text for exactly that image, save
     `{"<slot id>": {"asset_sha256": "<pick asset_sha256>", "text": "..."}}`
     outside W and run again with `--alt-texts <file>` (the batch resumes; nothing
     is regenerated). `awaiting_authoring` / `authored_stale` / `authored_invalid`:
     write `<W>/<slots_dir>/<slot>.svg` to the report's `authoring` contract
     (viewBox, `data-slot-spec-sha256`, the `<metadata>` marker, colours only
     `currentColor` / `var(--qontinui-*)`). `icon_unknown`: name a lucide icon for
     the slot in the target.
   - `2`: a gate refused (D5/D6/D7, or a `foreign_file`): report the gate and its
     detail; never copy files by hand.
   - `3`: generation deferred or failed (the host's bulk window, `busy`): run
     again later; the batch resumes.
   - `6`: image generation is switched off or refused (`image_generation_off`,
     `image_generation_local_only`, `gateway_has_no_image_route`). Stop.
   - `8`: some slot's state is UNKNOWN (an unreadable input). Report it as
     UNKNOWN, never as fresh.
4. When the report says `files_written: true`: in W, `git status` must show
   changes only under `<publish_dir>` (and any SVG you authored under
   `<slots_dir>`). Commit those, push B, and open the PR with the evidence as its
   body:

   ```bash
   gh pr create --base main --head B --title "feat(images): ..." \
     --body-file <studio root>/.studio/compile/<id>/evidence.md
   ```

   The merge train lands it like any other PR. Never merge it yourself.
5. When nothing was written, hand the worktree back:
   `bash <workspace-root>/qontinui-claude-config/scripts/allocate-worktree.sh --done --path <W> --abandon`.

The overnight re-render (`studio compile --changed`) is the runner-scheduler task
`studio compile-schedule --target <id> --install` registers; it follows these
same steps unattended.
