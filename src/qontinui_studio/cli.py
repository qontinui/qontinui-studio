"""``studio`` — the command line.

Exit codes: 0 ok; 1 config/usage error; 2 publication refused by a gate;
3 generation deferred (quota spent) or failed.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from qontinui_studio import postprocess
from qontinui_studio.adapters import build_adapters
from qontinui_studio.briefs import load_brief, validate_brief
from qontinui_studio.errors import StudioError
from qontinui_studio.library import Library
from qontinui_studio.notify import CoordFindingNotifier, FileNotifier
from qontinui_studio.pipeline import BatchOutcome, Context, resume_all, run_brief
from qontinui_studio.providers import load_providers
from qontinui_studio.publish import publish_batch, unpublish_batch
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
        sites=load_sites(ws.sites_dir),
        adapters=build_adapters(inventory),
        notifier=CoordFindingNotifier(file_notifier),
    )


def _report(outcome: BatchOutcome) -> int:
    click.echo(f"batch {outcome.batch_id}: {outcome.status}")
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
    sites = load_sites(ws.sites_dir)
    bad = 0
    for p in paths:
        problems = validate_brief(load_brief(p).brief, sites, inventory)
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
    sys.exit(_report(run_brief(ctx, path, publish=not no_publish)))


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
    result = publish_batch(ctx.conn, ctx.sites, batch_id, ctx.notifier, ctx.clock, ctx.probe)
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
        removed = unpublish_batch(ctx.conn, ctx.sites, batch_id, ctx.notifier, ctx.clock)
    except KeyError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"unpublished {batch_id}: removed {len(removed)} file(s)")


@main.group()
def library() -> None:
    """The asset library."""


@library.command("list")
@click.option("--batch", "batch_id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def library_list(obj: Obj, batch_id: str | None, as_json: bool) -> None:
    ws = Workspace(obj.root)
    lib = Library(connect(ws.db_path))
    assets = lib.assets(batch_id)
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
            f"{a.uid}  {a.provider}/{a.model}  commercial={a.licence_commercial_use} "
            f"reviewed={a.licence_tos_reviewed or 'UNREVIEWED'}  watermark={a.watermark}  "
            f"ai_generated={a.ai_generated}  variants={len(lib.variants(a.uid))}"
        )


@main.group()
def review() -> None:
    """The static review page."""


@review.command("build")
@click.pass_obj
def review_build(obj: Obj) -> None:
    ws = Workspace(obj.root)
    page = build_review_page(connect(ws.db_path), ws.review_dir, utc_now())
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


def run() -> None:
    try:
        main(standalone_mode=True)
    except StudioError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    run()
