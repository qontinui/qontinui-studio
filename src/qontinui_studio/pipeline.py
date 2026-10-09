"""brief -> scheduled generation -> library -> slot crops -> gated publication.

``run_brief`` is the zero-manual-step path: one call takes a brief file to
published, labelled, provenance-carrying slot assets on every target site, or
to a refusal notice naming the gate that stopped it.

Ingest is tracked per job (``downloaded`` -> ``ingested``), so a crash between
download and ingest is repaired by the next run rather than leaving a batch
with silently missing slots.
"""

from __future__ import annotations

import json
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
from qontinui_studio.gates import (
    ManifestFetch,
    PageHealthProbe,
    http_manifest_fetch,
    http_page_health_probe,
)
from qontinui_studio.library import Library, TermsSnapshot
from qontinui_studio.notify import Notifier
from qontinui_studio.postprocess import crop_to_slot, export, remove_background
from qontinui_studio.providers import ProviderInventory
from qontinui_studio.publish import PublishResult, publish_batch, recover
from qontinui_studio.scheduler import RetryPolicy, Scheduler, utc_now
from qontinui_studio.sites import Site
from qontinui_studio.workspace import Workspace


def _sleep(seconds: float) -> None:
    # Looked up at call time so tests can patch ``time.sleep``.
    time.sleep(seconds)


def _now() -> datetime:
    # Looked up at call time so tests can patch the clock.
    return utc_now()


@dataclass
class BatchOutcome:
    batch_id: str
    status: str
    completed_jobs: list[str] = field(default_factory=list)
    failed_jobs: list[str] = field(default_factory=list)
    deferred_jobs: list[str] = field(default_factory=list)
    unavailable_providers: dict[str, str] = field(default_factory=dict)
    publish: PublishResult | None = None
    error: str | None = None


@dataclass
class Context:
    workspace: Workspace
    conn: sqlite3.Connection
    inventory: ProviderInventory
    sites: dict[str, Site]
    adapters: AdapterSet
    notifier: Notifier
    clock: Callable[[], datetime] = _now
    sleep: Callable[[float], None] = _sleep
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    probe: PageHealthProbe = http_page_health_probe
    fetch: ManifestFetch = http_manifest_fetch

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
    stamp = now.strftime("%Y%m%dT%H%M%S")
    batch_id = f"{loaded.brief.id}-{stamp}"
    suffix = 1
    while ctx.conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone():
        suffix += 1
        batch_id = f"{loaded.brief.id}-{stamp}-{suffix}"
    targets = [[t.site, t.slot] for t in loaded.brief.targets if t.is_slot]
    ctx.conn.execute(
        "INSERT INTO batches (id, brief_id, brief_path, brief_sha256, targets_json, created_at, "
        "status) VALUES (?,?,?,?,?,?, 'generating')",
        (
            batch_id,
            loaded.brief.id,
            str(loaded.path),
            loaded.sha256,
            json.dumps(targets),
            now.isoformat(),
        ),
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


def ingest(ctx: Context, loaded: LoadedBrief, job: sqlite3.Row) -> None:
    """Record a downloaded job's images in the library, cut every target's
    variants, then mark the job ``ingested`` — all in one transaction, so a
    crash leaves the job ``downloaded`` with no partial rows."""
    library = Library(ctx.conn)
    # Exactly what was sent and the terms as reviewed at submission (D5), as
    # persisted on the job when it was claimed.
    terms = TermsSnapshot.from_json(job["terms_json"])
    model = str(job["model"])
    prompt = str(job["prompt"])
    files = [Path(p) for p in json.loads(job["files_json"])]
    brief = loaded.brief
    out_dir = ctx.workspace.variants_dir / job["batch_id"]
    try:
        for n, raw in enumerate(files):
            uid = f"{job['id']}-{n}"
            asset = library.add_asset(
                uid=uid,
                asset_id=brief.id,
                brief_id=brief.id,
                batch_id=job["batch_id"],
                job_id=job["id"],
                terms=terms,
                model=model,
                prompt=prompt,
                created_at=ctx.clock(),
                raw_path=raw,
                commit=False,
            )
            with Image.open(raw) as opened:
                keep_alpha = opened.mode in ("RGBA", "LA", "P")
                source = opened.convert("RGBA" if keep_alpha else "RGB")
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
                            commit=False,
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
                        commit=False,
                    )
        ctx.conn.execute(
            "UPDATE jobs SET status = 'ingested', updated_at = ? WHERE id = ?",
            (ctx.clock().isoformat(), job["id"]),
        )
        ctx.conn.commit()
    except BaseException:
        ctx.conn.rollback()
        raise


def _fail_batch(ctx: Context, batch_id: str, reason: str) -> None:
    ctx.conn.execute(
        "UPDATE batches SET status = 'failed', reason = ? WHERE id = ?", (reason, batch_id)
    )
    ctx.conn.execute(
        "UPDATE jobs SET status = 'failed', last_error = ? "
        "WHERE batch_id = ? AND status IN ('queued', 'submitting', 'submitted', 'downloaded')",
        (reason, batch_id),
    )
    ctx.conn.commit()


def drive_batch(ctx: Context, batch_id: str, publish: bool = True) -> BatchOutcome:
    row = ctx.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ConfigError(f"no batch '{batch_id}'")
    loaded = load_brief(Path(row["brief_path"]), root=ctx.workspace.root)
    if loaded.sha256 != row["brief_sha256"]:
        reason = (
            f"brief {row['brief_path']} changed since batch {batch_id} was created; "
            "run the brief again to start a new batch"
        )
        _fail_batch(ctx, batch_id, reason)
        raise ConfigError(reason)
    outcome = BatchOutcome(batch_id=batch_id, status=row["status"])
    outcome.unavailable_providers = dict(ctx.adapters.unavailable)
    ctx.scheduler().run({batch_id: _request_for(loaded)}, sleep=ctx.sleep)
    for job in ctx.conn.execute(
        "SELECT * FROM jobs WHERE batch_id = ? AND status = 'downloaded' ORDER BY id", (batch_id,)
    ).fetchall():
        try:
            ingest(ctx, loaded, job)
        except OSError:
            raise  # local I/O: transient; the job stays 'downloaded' for the next run
        except Exception as e:
            # Permanent (undecodable image, missing tool, removed site/slot):
            # fail THIS job with the reason; the batch carries on without it.
            ctx.conn.execute(
                "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                (f"ingest failed: {type(e).__name__}: {e}", ctx.clock().isoformat(), job["id"]),
            )
            ctx.conn.commit()
    jobs = ctx.conn.execute(
        "SELECT id, status FROM jobs WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    outcome.completed_jobs = [j["id"] for j in jobs if j["status"] == "ingested"]
    outcome.failed_jobs = [j["id"] for j in jobs if j["status"] == "failed"]
    outcome.deferred_jobs = [
        j["id"] for j in jobs if j["status"] in ("queued", "submitting", "submitted", "downloaded")
    ]
    if outcome.deferred_jobs:
        outcome.status = "generating"
        return outcome
    if not outcome.completed_jobs:
        reasons = "; ".join(
            str(r["last_error"])
            for r in ctx.conn.execute(
                "SELECT last_error FROM jobs WHERE batch_id = ? AND last_error IS NOT NULL",
                (batch_id,),
            )
        )
        ctx.conn.execute(
            "UPDATE batches SET status = 'failed', reason = ? WHERE id = ?",
            (reasons or "every job failed", batch_id),
        )
        ctx.conn.commit()
        outcome.status = "failed"
        return outcome
    ctx.conn.execute("UPDATE batches SET status = 'generated' WHERE id = ?", (batch_id,))
    ctx.conn.commit()
    outcome.status = "generated"
    if publish:
        outcome.publish = publish_batch(
            ctx.conn, ctx.sites, batch_id, ctx.notifier, ctx.clock, ctx.probe, ctx.fetch
        )
        outcome.status = "published" if outcome.publish.published else "refused"
    return outcome


def run_brief(ctx: Context, brief_path: Path, publish: bool = True) -> BatchOutcome:
    loaded = load_brief(brief_path, root=ctx.workspace.root)
    problems = validate_brief(loaded.brief, ctx.sites, ctx.inventory)
    if problems:
        raise ConfigError(f"{brief_path}: " + "; ".join(problems))
    batch_id = new_batch(ctx, loaded)
    return drive_batch(ctx, batch_id, publish=publish)


def resume_all(ctx: Context, publish: bool = True) -> list[BatchOutcome]:
    """Resume every generating batch. One batch's error never stops the others."""
    rows = ctx.conn.execute(
        "SELECT id FROM batches WHERE status = 'generating' ORDER BY created_at"
    ).fetchall()
    outcomes: list[BatchOutcome] = []
    # Finish or undo any publish a crash interrupted before resuming anything.
    recover(ctx.conn, ctx.sites)
    for r in rows:
        try:
            outcomes.append(drive_batch(ctx, r["id"], publish=publish))
        except Exception as e:  # one bad batch never stops the others
            ctx.conn.rollback()
            status = ctx.conn.execute(
                "SELECT status FROM batches WHERE id = ?", (r["id"],)
            ).fetchone()["status"]
            outcomes.append(
                BatchOutcome(batch_id=r["id"], status=status, error=f"{type(e).__name__}: {e}")
            )
    return outcomes
