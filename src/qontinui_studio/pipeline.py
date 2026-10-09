"""brief -> scheduled generation -> library -> slot crops -> gated publication.

``run_brief`` is the zero-manual-step path: one call takes a brief file to
published, labelled, provenance-carrying slot assets on every target site, or
to a refusal notice naming the gate that stopped it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from qontinui_studio.adapters import AdapterSet, GenerationRequest
from qontinui_studio.briefs import LoadedBrief, load_brief, validate_brief
from qontinui_studio.errors import ConfigError
from qontinui_studio.gates import PageHealthProbe, http_page_health_probe
from qontinui_studio.library import Library
from qontinui_studio.notify import Notifier
from qontinui_studio.postprocess import crop_to_slot, export, remove_background
from qontinui_studio.providers import ProviderInventory
from qontinui_studio.publish import PublishResult, publish_batch
from qontinui_studio.scheduler import CompletedJob, RetryPolicy, Scheduler, utc_now
from qontinui_studio.sites import Site
from qontinui_studio.workspace import Workspace


@dataclass
class BatchOutcome:
    batch_id: str
    status: str
    completed_jobs: list[str] = field(default_factory=list)
    failed_jobs: list[str] = field(default_factory=list)
    deferred_jobs: list[str] = field(default_factory=list)
    unavailable_providers: dict[str, str] = field(default_factory=dict)
    publish: PublishResult | None = None


@dataclass
class Context:
    workspace: Workspace
    conn: sqlite3.Connection
    inventory: ProviderInventory
    sites: dict[str, Site]
    adapters: AdapterSet
    notifier: Notifier
    clock: Callable[[], datetime] = utc_now
    sleep: Callable[[float], None] = time.sleep
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    probe: PageHealthProbe = http_page_health_probe

    def scheduler(self) -> Scheduler:
        return Scheduler(
            self.conn,
            self.inventory,
            self.adapters,
            self.workspace.staging_dir,
            self.workspace.raw_dir,
            clock=self.clock,
            policy=self.policy,
        )


def new_batch(ctx: Context, loaded: LoadedBrief) -> str:
    now = ctx.clock()
    batch_id = f"{loaded.brief.id}-{now.strftime('%Y%m%dT%H%M%S')}"
    suffix = 1
    while ctx.conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone():
        suffix += 1
        batch_id = f"{loaded.brief.id}-{now.strftime('%Y%m%dT%H%M%S')}-{suffix}"
    ctx.conn.execute(
        "INSERT INTO batches (id, brief_id, brief_path, brief_sha256, created_at, status) "
        "VALUES (?,?,?,?,?, 'generating')",
        (batch_id, loaded.brief.id, str(loaded.path), loaded.sha256, now.isoformat()),
    )
    ctx.conn.commit()
    ctx.scheduler().enqueue(batch_id, loaded.brief.variants)
    return batch_id


def _request_for(loaded: LoadedBrief) -> tuple[list[str], GenerationRequest]:
    b = loaded.brief
    return (
        b.provider_preference,
        GenerationRequest(
            job_id="",
            prompt=b.full_prompt(),
            aspect_ratio=b.aspect_ratio,
            reference_images=list(b.reference_images),
        ),
    )


def ingest(ctx: Context, loaded: LoadedBrief, job: CompletedJob) -> None:
    """Record a completed job's image in the library and cut every target's variants."""
    library = Library(ctx.conn)
    provider = ctx.inventory.get(job.provider)
    brief = loaded.brief
    for n, raw in enumerate(job.files):
        uid = f"{job.job_id}-{n}"
        asset = library.add_asset(
            uid=uid,
            asset_id=brief.id,
            brief_id=brief.id,
            batch_id=job.batch_id,
            job_id=job.job_id,
            provider=provider,
            model=job.model,
            prompt=brief.full_prompt(),
            created_at=ctx.clock(),
            raw_path=raw,
        )
        with Image.open(raw) as opened:
            source = (
                opened.convert("RGBA")
                if opened.mode in ("RGBA", "LA", "P")
                else opened.convert("RGB")
            )
        out_dir = ctx.workspace.variants_dir / job.batch_id
        for target in brief.targets:
            if target.is_slot:
                site = ctx.sites[target.site or ""]
                slot = site.slots[target.slot or ""]
                img = source
                if slot.remove_background:
                    img = remove_background(img)
                img = crop_to_slot(img, slot.width, slot.height, (slot.focal_x, slot.focal_y))
                for fmt in slot.formats:
                    path = export(img, out_dir / f"{uid}--{site.id}--{target.slot}", fmt)
                    library.add_variant(
                        variant_id=f"{uid}:{site.id}:{target.slot}:{fmt}",
                        asset_uid=asset.uid,
                        site=site.id,
                        slot=target.slot,
                        campaign=None,
                        fmt=fmt,
                        path=path,
                        created_at=ctx.clock(),
                    )
            else:
                path = export(source, out_dir / f"{uid}--campaign--{target.campaign}", "webp")
                library.add_variant(
                    variant_id=f"{uid}:campaign:{target.campaign}:webp",
                    asset_uid=asset.uid,
                    site=None,
                    slot=None,
                    campaign=target.campaign,
                    fmt="webp",
                    path=path,
                    created_at=ctx.clock(),
                )


def drive_batch(ctx: Context, batch_id: str, publish: bool = True) -> BatchOutcome:
    row = ctx.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ConfigError(f"no batch '{batch_id}'")
    loaded = load_brief(Path(row["brief_path"]))
    outcome = BatchOutcome(batch_id=batch_id, status=row["status"])
    outcome.unavailable_providers = dict(ctx.adapters.unavailable)
    sched = ctx.scheduler()
    report = sched.run({batch_id: _request_for(loaded)}, sleep=ctx.sleep)
    for job in report.completed:
        ingest(ctx, loaded, job)
    jobs = ctx.conn.execute(
        "SELECT id, status FROM jobs WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    outcome.completed_jobs = [j["id"] for j in jobs if j["status"] == "done"]
    outcome.failed_jobs = [j["id"] for j in jobs if j["status"] == "failed"]
    outcome.deferred_jobs = [j["id"] for j in jobs if j["status"] in ("queued", "submitted")]
    if outcome.deferred_jobs:
        outcome.status = "generating"
        return outcome
    if not outcome.completed_jobs:
        ctx.conn.execute("UPDATE batches SET status = 'failed' WHERE id = ?", (batch_id,))
        ctx.conn.commit()
        outcome.status = "failed"
        return outcome
    ctx.conn.execute("UPDATE batches SET status = 'generated' WHERE id = ?", (batch_id,))
    ctx.conn.commit()
    outcome.status = "generated"
    if publish:
        outcome.publish = publish_batch(
            ctx.conn, ctx.sites, batch_id, ctx.notifier, ctx.clock, ctx.probe
        )
        outcome.status = "published" if outcome.publish.published else "refused"
    return outcome


def run_brief(ctx: Context, brief_path: Path, publish: bool = True) -> BatchOutcome:
    loaded = load_brief(brief_path)
    problems = validate_brief(loaded.brief, ctx.sites, ctx.inventory)
    if problems:
        raise ConfigError(f"{brief_path}: " + "; ".join(problems))
    batch_id = new_batch(ctx, loaded)
    return drive_batch(ctx, batch_id, publish=publish)


def resume_all(ctx: Context, publish: bool = True) -> list[BatchOutcome]:
    rows = ctx.conn.execute(
        "SELECT id FROM batches WHERE status = 'generating' ORDER BY created_at"
    ).fetchall()
    return [drive_batch(ctx, r["id"], publish=publish) for r in rows]
