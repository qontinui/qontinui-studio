"""The whole launch kit, end to end: record -> capture -> decoration -> bundle
(-> gated publication). This is what ``studio launch build`` / ``run`` and the
coord-triggered ``launch-kit`` skill execute.

Every input but the record is optional, and each missing one becomes a note in
the bundle's manifest rather than a failed launch. The record is not optional
for PUBLICATION (``d1_record``), but a bundle without one still assembles, so
the operator can see what the kit would publish.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from qontinui_studio.errors import ConfigError, StudioError
from qontinui_studio.launch import decor
from qontinui_studio.launch.bundle import MANIFEST, BundleResult, assemble, check_out_dir
from qontinui_studio.launch.capture import CaptureResult, capture, load_script, serve_directory
from qontinui_studio.launch.product import LaunchProduct, load_product
from qontinui_studio.launch.publish import LaunchPublishResult, publish_launch, register_launch
from qontinui_studio.launch.record import (
    BuildRecordClient,
    BuildRecordInvalid,
    FetchedRecord,
    FetchOutcome,
    identity_violations,
    load_record_file,
)
from qontinui_studio.launch.video import ReplaySegment
from qontinui_studio.pipeline import Context
from qontinui_studio.review import build_review_page


@dataclass
class KitOptions:
    record_file: Path | None = None
    generate_decor: bool = True
    capture: bool = True
    video: bool = True
    replay: ReplaySegment | None = None
    out_dir: Path | None = None


@dataclass
class KitRun:
    product: LaunchProduct
    launch_id: str
    bundle: BundleResult
    publish: LaunchPublishResult | None = None


def check_out_target(ctx: Context, out_dir: Path, product: LaunchProduct) -> None:
    """Where ``--out`` may write. Refused (:class:`ConfigError`) when it equals
    or overlaps any product's or site's publish/preview dir, or any registered
    launch's publish or bundle dir; when it holds the studio's lock or a
    journal; or when it holds a bundle that a launch row owns. A bundle is
    built once per launch; a rebuild gets a fresh directory."""
    check_out_dir(out_dir)
    target = out_dir.resolve()
    owned: list[tuple[str, Path]] = []
    for site in ctx.sites.values():
        owned.append((f"site '{site.id}' publish_dir", site.publish_dir))
        if site.preview_dir is not None:
            owned.append((f"site '{site.id}' preview_dir", site.preview_dir))
    products = [product]
    products_dir = ctx.workspace.products_dir
    if products_dir.is_dir():
        for path in sorted(products_dir.glob("*.yaml")):
            try:
                products.append(load_product(path, ctx.workspace.root))
            except ConfigError:
                continue  # a capture script or a broken definition: not a product
    for p in products:
        owned.append((f"product '{p.slug}' publish_dir", p.publish.publish_dir))
        if p.publish.preview_dir is not None:
            owned.append((f"product '{p.slug}' preview_dir", p.publish.preview_dir))
    for row in ctx.conn.execute("SELECT id, publish_dir, bundle_dir FROM launches").fetchall():
        if row["publish_dir"]:
            owned.append((f"launch {row['id']} publish_dir", Path(row["publish_dir"])))
        owned.append((f"launch {row['id']} bundle", Path(row["bundle_dir"])))
    for what, directory in owned:
        d = directory.resolve()
        if target == d or target.is_relative_to(d) or d.is_relative_to(target):
            raise ConfigError(f"refusing --out {out_dir}: it overlaps {what} ({directory})")
    manifest = out_dir / MANIFEST
    if manifest.is_file():
        try:
            launch_id = json.loads(manifest.read_text(encoding="utf-8")).get("launch_id")
        except (OSError, ValueError, AttributeError):
            launch_id = None
        if (
            launch_id
            and ctx.conn.execute("SELECT 1 FROM launches WHERE id = ?", (launch_id,)).fetchone()
        ):
            raise ConfigError(
                f"refusing --out {out_dir}: it holds launch {launch_id}'s bundle; a bundle is "
                "never rebuilt in place"
            )


def new_launch_id(ctx: Context, slug: str) -> str:
    base = f"{slug}-{ctx.clock().strftime('%Y%m%dT%H%M%S')}"
    launch_id, n = base, 1
    while ctx.conn.execute("SELECT 1 FROM launches WHERE id = ?", (launch_id,)).fetchone():
        n += 1
        launch_id = f"{base}-{n}"
    return launch_id


def fetch_record(
    product: LaunchProduct, options: KitOptions, client: BuildRecordClient | None
) -> FetchOutcome:
    """The product's record, refused (:class:`BuildRecordInvalid`) unless it is
    THIS product's: matching slugs, and only repos the product declares."""
    if options.record_file is not None:
        outcome: FetchOutcome = load_record_file(options.record_file)
    else:
        client = client or BuildRecordClient(web_base=product.record.web_base)
        outcome = client.fetch(product.slug, product.record.door)
    if isinstance(outcome, FetchedRecord):
        problems = identity_violations(outcome, product.slug, product.repos)
        if problems:
            raise BuildRecordInvalid(problems)
    return outcome


def run_capture(
    product: LaunchProduct, ctx: Context, out_dir: Path, clock: Callable[[], datetime]
) -> tuple[CaptureResult | None, str | None]:
    cfg = product.capture
    if cfg is None:
        return None, "no capture configured; the bundle has no screenshots or walkthrough"
    try:
        script = load_script(cfg.script)
        if cfg.build_review_page and cfg.serve_dir is not None:
            # The PUBLIC variant: it is captured into published screenshots.
            build_review_page(
                ctx.conn, cfg.serve_dir, clock(), root=ctx.workspace.root, public=True
            )
        if cfg.serve_dir is not None:
            with serve_directory(cfg.serve_dir) as base_url:
                return capture(script, base_url, out_dir, cfg.record_video), None
        assert cfg.base_url is not None  # one of the two, by the model's validator
        return capture(script, cfg.base_url, out_dir, cfg.record_video), None
    except StudioError as e:
        return None, f"capture skipped: {e}"


def run_kit(
    ctx: Context,
    product_path: Path,
    options: KitOptions | None = None,
    client: BuildRecordClient | None = None,
    publish: bool = False,
) -> KitRun:
    options = options or KitOptions()
    product = load_product(product_path, ctx.workspace.root)
    product.publish.check_sites(ctx.sites)
    if options.out_dir is not None:
        check_out_target(ctx, options.out_dir, product)  # before any work
    outcome = fetch_record(product, options, client)

    notes: list[str] = []
    if options.generate_decor and product.brief is not None:
        notes += decor.generate(ctx, product)
    decor_outcome = decor.collect(product, ctx.sites)
    decor_outcome.notes[:0] = notes

    launch_id = new_launch_id(ctx, product.slug)
    work = ctx.workspace.launches_dir / ".capture" / launch_id
    shots: CaptureResult | None = None
    capture_note: str | None = "capture disabled for this run"
    if options.capture:
        shots, capture_note = run_capture(product, ctx, work, ctx.clock)

    out_dir = options.out_dir or ctx.workspace.launches_dir / launch_id
    check_out_target(ctx, out_dir, product)  # again: the world may have moved
    bundle = assemble(
        product,
        outcome,
        out_dir,
        launch_id,
        ctx.clock,
        capture=shots,
        capture_note=capture_note,
        decor=decor_outcome,
        replay=options.replay,
        make_video=options.video,
    )
    shutil.rmtree(work, ignore_errors=True)
    register_launch(ctx.conn, bundle.manifest, product_path, out_dir, bundle.notes)
    run = KitRun(product=product, launch_id=launch_id, bundle=bundle)
    if publish:
        run.publish = publish_launch(
            ctx.conn,
            launch_id,
            product,
            ctx.notifier,
            ctx.clock,
            ctx.bridge,
            ctx.fetch,
            ctx.workspace.root,
            ctx.sleep,
            records=client,
        )
    return run
