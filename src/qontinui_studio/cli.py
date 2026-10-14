"""``studio`` — the command line.

Exit codes: 0 ok; 1 config/usage error; 2 publication refused by a gate;
3 generation deferred (quota spent) or failed; 4 no build record yet (the
factory's record route answered 404 or was unreachable); 5 a launch trigger was
registered but its read-back did not confirm it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click

from qontinui_studio import postprocess
from qontinui_studio.adapters import build_adapters
from qontinui_studio.briefs import load_brief, validate_brief
from qontinui_studio.errors import ImportRefused, PublicationRefused, StudioError
from qontinui_studio.inbox import import_inbox
from qontinui_studio.launch.facts import derive_facts
from qontinui_studio.launch.kit import KitOptions, KitRun, run_capture, run_kit
from qontinui_studio.launch.product import LaunchProduct, load_product
from qontinui_studio.launch.publish import (
    LaunchNotPublished,
    LaunchPublishResult,
    publish_launch,
    recover_all_launches,
    unpublish_launch,
)
from qontinui_studio.launch.record import WEB_BASE, BuildRecordClient, NoBuildRecord
from qontinui_studio.launch.trigger import TriggerError, register_trigger
from qontinui_studio.launch.video import FileReplaySegment
from qontinui_studio.library import APPROVAL_STATUSES, Library, export_yaml
from qontinui_studio.notify import COORD_BASE, CoordFindingNotifier, FileNotifier
from qontinui_studio.pipeline import BatchOutcome, Context, resume_all, run_brief
from qontinui_studio.providers import load_providers
from qontinui_studio.publish import atomic_write, publish_batch, unpublish_batch
from qontinui_studio.review import build_review_page
from qontinui_studio.scheduler import utc_now
from qontinui_studio.sites import load_sites
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace


@dataclass
class Obj:
    root: Path


def _context(root: Path) -> Context:
    ws = Workspace(root)
    inventory = load_providers(ws.providers_path)
    file_notifier = FileNotifier(ws.notices_dir)
    return Context(
        workspace=ws,
        conn=connect(ws.db_path),
        inventory=inventory,
        sites=load_sites(ws.sites_dir, root),
        adapters=build_adapters(inventory),
        notifier=CoordFindingNotifier(file_notifier),
    )


def _report(outcome: BatchOutcome) -> int:
    click.echo(f"batch {outcome.batch_id}: {outcome.status}")
    if outcome.error:
        click.echo(f"  error: {outcome.error}")
        return 1
    if not outcome.driven:
        click.echo(f"  {outcome.note}")
        return 0
    for pid, why in sorted(outcome.unavailable_providers.items()):
        click.echo(f"  provider {pid} off: {why}")
    for j in outcome.failed_jobs:
        click.echo(f"  job {j}: failed")
    for j in outcome.deferred_jobs:
        click.echo(f"  job {j}: deferred (quota or backoff)")
    if outcome.publish is not None:
        for g in outcome.publish.gate_results:
            if g.outcome != "pass":
                click.echo(f"  gate {g.gate} {g.outcome}: {g.subject}: {g.detail}")
        if outcome.publish.delivery is not None:
            d = outcome.publish.delivery
            click.echo(
                f"  notice -> {d.channel}: {d.location}" + (f" ({d.note})" if d.note else "")
            )
    if outcome.status == "refused":
        return 2
    if outcome.status in ("failed", "generating"):
        return 3
    return 0


@click.group()
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Studio root holding providers.yaml, sites/ and .studio/",
)
@click.pass_context
def main(ctx: click.Context, root: Path) -> None:
    """qontinui-studio asset pipeline."""
    ctx.obj = Obj(root=root.resolve())


@main.group()
def brief() -> None:
    """Validate and run briefs."""


@brief.command("validate")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.pass_obj
def brief_validate(obj: Obj, paths: tuple[Path, ...]) -> None:
    ws = Workspace(obj.root)
    inventory = load_providers(ws.providers_path)
    sites = load_sites(ws.sites_dir, obj.root)
    bad = 0
    for p in paths:
        problems = validate_brief(load_brief(p, root=obj.root).brief, sites, inventory)
        for problem in problems:
            click.echo(f"{p}: {problem}")
        bad += bool(problems)
        if not problems:
            click.echo(f"{p}: ok")
    sys.exit(1 if bad else 0)


@brief.command("run")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--no-publish", is_flag=True, help="Generate and cut variants, but do not publish.")
@click.pass_obj
def brief_run(obj: Obj, path: Path, no_publish: bool) -> None:
    """Generate, post-process and (gate-)publish one brief."""
    ctx = _context(obj.root)
    try:
        outcome = run_brief(ctx, path, publish=not no_publish)
    except StudioError as e:
        raise click.ClickException(str(e)) from e
    sys.exit(_report(outcome))


@main.group()
def jobs() -> None:
    """The persisted job queue."""


@jobs.command("run")
@click.option("--no-publish", is_flag=True)
@click.pass_obj
def jobs_run(obj: Obj, no_publish: bool) -> None:
    """Resume every batch still generating (e.g. deferred to today's quota)."""
    ctx = _context(obj.root)
    outcomes = resume_all(ctx, publish=not no_publish)
    if not outcomes:
        click.echo("no batches are generating")
    sys.exit(max((_report(o) for o in outcomes), default=0))


@main.command("publish")
@click.option("--batch", "batch_id", required=True)
@click.pass_obj
def publish_cmd(obj: Obj, batch_id: str) -> None:
    """Run the D5/D6/D7 gates over a generated batch and publish it if they pass."""
    ctx = _context(obj.root)
    try:
        result = publish_batch(
            ctx.conn,
            ctx.sites,
            batch_id,
            ctx.notifier,
            ctx.clock,
            bridge=ctx.bridge,
            fetch=ctx.fetch,
            root=obj.root,
            sleep=ctx.sleep,
        )
    except PublicationRefused as e:
        click.echo(f"refused: {e}", err=True)
        sys.exit(2)
    except StudioError as e:
        raise click.ClickException(str(e)) from e
    for g in result.gate_results:
        click.echo(f"{g.outcome:15} {g.gate:15} {g.subject}: {g.detail}")
    if result.delivery:
        click.echo(f"notice -> {result.delivery.channel}: {result.delivery.location}")
    sys.exit(0 if result.published else 2)


@main.command("unpublish")
@click.option("--batch", "batch_id", required=True)
@click.pass_obj
def unpublish_cmd(obj: Obj, batch_id: str) -> None:
    """Remove a batch's published files and manifest entries (one step)."""
    ctx = _context(obj.root)
    try:
        removed = unpublish_batch(
            ctx.conn,
            ctx.sites,
            batch_id,
            ctx.notifier,
            ctx.clock,
            root=obj.root,
            bridge=ctx.bridge,
            fetch=ctx.fetch,
            sleep=ctx.sleep,
        )
    except KeyError as e:
        raise click.ClickException(str(e)) from e
    except PublicationRefused as e:
        click.echo(f"refused: {e}", err=True)
        sys.exit(2)
    except StudioError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"unpublished {batch_id}: removed {len(removed)} file(s)")


@main.group()
def library() -> None:
    """The asset library."""


@library.command("list")
@click.option("--batch", "batch_id", default=None)
@click.option(
    "--status",
    "statuses",
    multiple=True,
    type=click.Choice(APPROVAL_STATUSES),
    help="Only assets with this approval status (repeatable).",
)
@click.option(
    "--origin",
    type=click.Choice(["generated", "imported"]),
    default=None,
    help="Only generated or only imported assets.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def library_list(
    obj: Obj, batch_id: str | None, statuses: tuple[str, ...], origin: str | None, as_json: bool
) -> None:
    """List library assets; filters combine (e.g. ``--status approved --json``
    is what a consumer project may use)."""
    ws = Workspace(obj.root)
    lib = Library(connect(ws.db_path))
    assets = [
        a
        for a in lib.assets(batch_id)
        if (not statuses or a.approval_status in statuses)
        and (origin is None or a.origin == origin)
    ]
    if as_json:
        click.echo(
            json.dumps(
                [
                    {**a.__dict__, "variants": [v.__dict__ for v in lib.variants(a.uid)]}
                    for a in assets
                ],
                indent=2,
            )
        )
        return
    for a in assets:
        click.echo(
            f"{a.uid}  {a.origin}  {a.provider}/{a.model}  commercial={a.licence_commercial_use} "
            f"reviewed={a.licence_tos_reviewed or 'UNREVIEWED'}  watermark={a.watermark}  "
            f"ai_generated={a.ai_generated}  approval={a.approval_status}  "
            f"variants={len(lib.variants(a.uid))}"
        )


@library.command("import")
@click.argument("inbox", type=click.Path(file_okay=False, path_type=Path))
@click.pass_obj
def library_import(obj: Obj, inbox: Path) -> None:
    """Import an inbox of externally made images described by <inbox>/manifest.yaml
    (contract: schemas/import-manifest.schema.json). All or nothing; never publishes."""
    ws = Workspace(obj.root)
    try:
        outcome = import_inbox(connect(ws.db_path), ws, inbox, utc_now)
    except ImportRefused as e:
        click.echo(f"refused: {e}", err=True)
        for line in e.errors:
            click.echo(f"  {line}", err=True)
        sys.exit(1)
    for line in outcome.warnings:
        click.echo(f"warning: {line}", err=True)
    for uid in outcome.imported:
        click.echo(f"imported {uid}")
    for uid in outcome.skipped:
        click.echo(f"unchanged {uid} (already imported)")
    click.echo(
        f"batch {outcome.batch_id}: {len(outcome.imported)} imported, "
        f"{len(outcome.skipped)} already present"
    )


def _decide(obj: Obj, uid: str, by: str, decision: str) -> None:
    lib = Library(connect(Workspace(obj.root).db_path))
    try:
        asset, changed = lib.decide(uid, decision, by, utc_now().date())
    except StudioError as e:
        raise click.ClickException(str(e)) from e
    verb = decision if changed else f"already {decision}"
    click.echo(f"{asset.uid}: {verb} by {asset.approval_by} on {asset.approval_on}")


@library.command("approve")
@click.argument("uid")
@click.option("--by", "by", required=True, help="Reviewer alias (never an e-mail address).")
@click.pass_obj
def library_approve(obj: Obj, uid: str, by: str) -> None:
    """Approve an asset for use by consumers. Refused unless its licence allows
    commercial use (D5). Not a publish gate: the automated gates publish (D7)."""
    _decide(obj, uid, by, "approved")


@library.command("reject")
@click.argument("uid")
@click.option("--by", "by", required=True, help="Reviewer alias (never an e-mail address).")
@click.pass_obj
def library_reject(obj: Obj, uid: str, by: str) -> None:
    """Reject an asset: consumers must not use it."""
    _decide(obj, uid, by, "rejected")


@library.command("export")
@click.option("--out", "out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.pass_obj
def library_export(obj: Obj, out: Path) -> None:
    """Write a deterministic YAML manifest of every asset (provenance, licence,
    approval, variants by SHA-256). No binaries, no local paths."""
    data = export_yaml(connect(Workspace(obj.root).db_path))
    atomic_write(out, data)
    click.echo(str(out))


@main.group()
def review() -> None:
    """The static review page."""


@review.command("build")
@click.pass_obj
def review_build(obj: Obj) -> None:
    ws = Workspace(obj.root)
    page = build_review_page(connect(ws.db_path), ws.review_dir, utc_now(), root=obj.root)
    click.echo(str(page))


@main.group()
def media() -> None:
    """Local video post-processing (ffmpeg)."""


@media.command("trim")
@click.argument("src", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("dest", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--start", type=float, default=0.0)
@click.option("--duration", type=float, required=True)
def media_trim(src: Path, dest: Path, start: float, duration: float) -> None:
    click.echo(str(postprocess.trim(src, dest, start, duration)))


@media.command("loop")
@click.argument("src", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("dest", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--count", type=int, default=2)
@click.option("--boomerang", is_flag=True, help="Forwards then reversed, for a seamless loop.")
def media_loop(src: Path, dest: Path, count: int, boomerang: bool) -> None:
    out = postprocess.boomerang(src, dest) if boomerang else postprocess.loop(src, dest, count)
    click.echo(str(out))


@main.group()
def providers() -> None:
    """The providers.yaml inventory."""


@providers.command("list")
@click.pass_obj
def providers_list(obj: Obj) -> None:
    inventory = load_providers(Workspace(obj.root).providers_path)
    for p in inventory.providers:
        why = p.schedulable_reason()
        click.echo(
            f"{p.id:12} automation={p.automation_allowed.value:7} commercial={p.commercial_use.value:7} "
            f"{'SCHEDULABLE' if why is None else 'off: ' + why}"
        )


# ---- launch kit (plan Phase 3) ------------------------------------------------------


@main.group()
def launch() -> None:
    """The launch kit: build record + capture + decoration -> a gated launch bundle."""


def _replay(path: Path | None) -> FileReplaySegment | None:
    return FileReplaySegment(path) if path is not None else None


def _kit_options(
    record_file: Path | None,
    no_generate: bool,
    no_capture: bool,
    no_video: bool,
    replay_clip: Path | None,
    out: Path | None,
) -> KitOptions:
    return KitOptions(
        record_file=record_file,
        generate_decor=not no_generate,
        capture=not no_capture,
        video=not no_video,
        replay=_replay(replay_clip),
        out_dir=out,
    )


def _kit_flags(f: Callable[..., None]) -> Callable[..., None]:
    for option in reversed(
        [
            click.option(
                "--record-file",
                type=click.Path(exists=True, dir_okay=False, path_type=Path),
                help="Read the build record from a local JSON file instead of the factory.",
            ),
            click.option(
                "--no-generate",
                is_flag=True,
                help="Do not run the product's brief; reuse published assets.",
            ),
            click.option(
                "--no-capture", is_flag=True, help="Skip screenshots and the walkthrough."
            ),
            click.option("--no-video", is_flag=True, help="Skip the film."),
            click.option(
                "--replay-clip",
                type=click.Path(exists=True, dir_okay=False, path_type=Path),
                help="A rendered build-record replay clip for the film's replay slot.",
            ),
            click.option("--out", type=click.Path(file_okay=False, path_type=Path), default=None),
        ]
    ):
        f = option(f)
    return f


def _report_kit(run: KitRun) -> None:
    m = run.bundle.manifest
    click.echo(f"launch {run.launch_id}: bundle at {run.bundle.out_dir}")
    rec = m["record"]
    if rec["present"]:
        click.echo(f"  record: {rec['source']} {rec['url']} sha256={rec['content_sha256']}")
    else:
        click.echo(f"  record: NONE ({rec['kind']}): {rec['detail']}")
    film = m.get("film")
    if film:
        click.echo(f"  film: {film['path']} {film['actual_seconds']}s")
        for name, why in film["omitted"].items():
            click.echo(f"    {name} {why}")
    for note in run.bundle.notes:
        click.echo(f"  note: {note}")


def _report_launch_publish(result: LaunchPublishResult) -> int:
    for g in result.gate_results:
        click.echo(f"{g.outcome:15} {g.gate:15} {g.subject}: {g.detail}")
    for w in result.warnings:
        click.echo(f"warning: {w}")
    if result.delivery:
        click.echo(f"notice -> {result.delivery.channel}: {result.delivery.location}")
    return 0 if result.published else 2


@launch.command("record")
@click.argument("slug")
@click.option(
    "--door", type=click.Choice(["public", "authed"]), default="public", show_default=True
)
@click.option("--web-base", default=WEB_BASE, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print the validated document.")
def launch_record(slug: str, door: str, web_base: str, as_json: bool) -> None:
    """Fetch and validate a product's build record."""
    outcome = BuildRecordClient(web_base=web_base).fetch(
        slug, "public" if door == "public" else "authed"
    )
    if isinstance(outcome, NoBuildRecord):
        click.echo(outcome.describe())
        sys.exit(4)
    if as_json:
        click.echo(json.dumps(outcome.document, indent=2, sort_keys=True))
        return
    click.echo(f"{outcome.url}: build-record/1, sha256 {outcome.content_sha256}")
    for fact in derive_facts(outcome.record):
        click.echo(f"  {fact.label:28} {fact.display()}   [{fact.source}]")


@launch.command("capture")
@click.argument("product_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.pass_obj
def launch_capture(obj: Obj, product_path: Path, out: Path) -> None:
    """Capture screenshots (and the walkthrough video) of the running product."""
    ctx = _context(obj.root)
    result, note = run_capture(load_product(product_path, obj.root), ctx, out, ctx.clock)
    if result is None:
        raise click.ClickException(note or "capture failed")
    for shot in result.screenshots:
        click.echo(f"{shot.path}  {shot.caption or ''}")
    click.echo(f"video: {result.video or result.video_note}")


@launch.command("build")
@click.argument("product_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_kit_flags
@click.pass_obj
def launch_build(
    obj: Obj,
    product_path: Path,
    record_file: Path | None,
    no_generate: bool,
    no_capture: bool,
    no_video: bool,
    replay_clip: Path | None,
    out: Path | None,
) -> None:
    """Assemble a launch bundle without publishing it."""
    ctx = _context(obj.root)
    options = _kit_options(record_file, no_generate, no_capture, no_video, replay_clip, out)
    run = run_kit(ctx, product_path, options)
    _report_kit(run)
    sys.exit(0 if run.bundle.has_record else 4)


@launch.command("run")
@click.argument("product_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_kit_flags
@click.pass_obj
def launch_run(
    obj: Obj,
    product_path: Path,
    record_file: Path | None,
    no_generate: bool,
    no_capture: bool,
    no_video: bool,
    replay_clip: Path | None,
    out: Path | None,
) -> None:
    """Assemble a launch bundle and publish it if every gate passes."""
    ctx = _context(obj.root)
    options = _kit_options(record_file, no_generate, no_capture, no_video, replay_clip, out)
    run = run_kit(ctx, product_path, options, publish=True)
    _report_kit(run)
    assert run.publish is not None
    code = _report_launch_publish(run.publish)
    sys.exit(4 if not run.bundle.has_record else code)


def _launch_product(ctx: Context, launch_id: str) -> LaunchProduct:
    row = ctx.conn.execute(
        "SELECT product_path FROM launches WHERE id = ?", (launch_id,)
    ).fetchone()
    if row is None:
        raise click.ClickException(f"no launch '{launch_id}'")
    product = load_product(Path(row["product_path"]), ctx.workspace.root)
    product.publish.check_sites(ctx.sites)
    return product


@launch.command("publish")
@click.option("--launch", "launch_id", required=True)
@click.pass_obj
def launch_publish(obj: Obj, launch_id: str) -> None:
    """Run the launch gates over an assembled bundle and publish it if they pass."""
    ctx = _context(obj.root)
    product = _launch_product(ctx, launch_id)
    result = publish_launch(
        ctx.conn,
        launch_id,
        product,
        ctx.notifier,
        ctx.clock,
        ctx.bridge,
        ctx.fetch,
        obj.root,
        ctx.sleep,
    )
    sys.exit(_report_launch_publish(result))


@launch.command("unpublish")
@click.option("--launch", "launch_id", required=True)
@click.pass_obj
def launch_unpublish(obj: Obj, launch_id: str) -> None:
    """Remove a published launch bundle (one step)."""
    ctx = _context(obj.root)
    try:
        removed = unpublish_launch(ctx.conn, launch_id, ctx.notifier, ctx.clock, obj.root)
    except (KeyError, LaunchNotPublished) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"unpublished {launch_id}: removed {len(removed)} file(s)")


@launch.command("recover")
@click.pass_obj
def launch_recover(obj: Obj) -> None:
    """Replay interrupted launch publishes/unpublishes, refuse orphaned ones and
    re-send missing published notices, for every launch directory."""
    ctx = _context(obj.root)
    lines, failed = recover_all_launches(ctx.conn, ctx.notifier, obj.root)
    for line in lines:
        click.echo(line)
    for d, why in sorted(failed.items()):
        click.echo(f"recovery FAILED for {d}: {why}")
    if not lines and not failed:
        click.echo("nothing to recover")
    sys.exit(1 if failed else 0)


@launch.command("list")
@click.pass_obj
def launch_list(obj: Obj) -> None:
    ws = Workspace(obj.root)
    for row in (
        connect(ws.db_path).execute("SELECT * FROM launches ORDER BY created_at DESC").fetchall()
    ):
        click.echo(f"{row['id']:40} {row['status']:12} {row['bundle_dir']}")


@launch.command("register-trigger")
@click.option("--work-unit-slug", required=True, help="The portfolio product's coord work unit.")
@click.option(
    "--product", "product_slug", required=True, help="products/<slug>.yaml the kit launches."
)
@click.option("--coord-base", default=COORD_BASE, show_default=True)
def launch_register_trigger(work_unit_slug: str, product_slug: str, coord_base: str) -> None:
    """Register the coord unit_status=shipped gate whose run_skill continuation runs the kit."""
    try:
        result = register_trigger(work_unit_slug, product_slug, base_url=coord_base)
    except TriggerError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"gate_id {result.gate_id}")
    click.echo(f"work_unit_id {result.work_unit_id}")
    if result.initial_verdict:
        click.echo(f"initial_verdict {result.initial_verdict}")
    for w in result.warnings:
        click.echo(f"warning {w}")
    click.echo(f"read_back {result.read_back}: {result.read_back_detail}")
    sys.exit(0 if result.read_back == "confirmed" else 5)


def run() -> None:
    try:
        main(standalone_mode=True)
    except StudioError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    run()
