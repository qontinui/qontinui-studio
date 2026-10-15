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
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

from qontinui_studio import provenance
from qontinui_studio.adapters import AdapterSet, GenerationRequest, SubmittedJob
from qontinui_studio.adapters.base import staged_provenance
from qontinui_studio.briefs import LoadedBrief, load_brief, validate_brief
from qontinui_studio.errors import ConfigError, LeaseLost, StudioError
from qontinui_studio.gates import (
    BridgeFactory,
    HttpUiBridge,
    ManifestFetch,
    http_manifest_fetch,
)
from qontinui_studio.library import Library, TermsSnapshot
from qontinui_studio.notify import Notifier
from qontinui_studio.postprocess import crop_to_slot, export, remove_background, sha256_file
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
    # Each unfinished or failed job's latest provider-side outcome
    # (``job_attempts``): ``busy``/``deferred``/``unknown``/``failed`` and why.
    last_attempts: dict[str, str] = field(default_factory=dict)
    # Set when generation stopped because this run had waited its cap in total
    # (a host answering ``busy`` again and again): why, and how to resume.
    stopped_waiting: str | None = None
    unavailable_providers: dict[str, str] = field(default_factory=dict)
    publish: PublishResult | None = None
    error: str | None = None
    # False when this call did not drive the batch (claimed elsewhere, or no
    # longer generating); ``note`` says why.
    driven: bool = True
    note: str | None = None


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
    bridge: BridgeFactory = HttpUiBridge
    fetch: ManifestFetch = http_manifest_fetch
    # Told about every wait before it is slept (seconds, why) — the CLI prints it.
    on_wait: Callable[[float, str], None] | None = None

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


def new_batch(ctx: Context, loaded: LoadedBrief, publish: bool = True) -> str:
    """Create a batch. ``publish=False`` records ``no_publish``: resume will
    generate it but never publish it."""
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
        "status, no_publish) VALUES (?,?,?,?,?,?, 'generating', ?)",
        (
            batch_id,
            loaded.brief.id,
            str(loaded.path),
            loaded.sha256,
            json.dumps(targets),
            now.isoformat(),
            0 if publish else 1,
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
            negative_prompt=b.negative_prompt.strip() if b.negative_prompt else None,
            seed=b.seed,
            recipe=b.recipe,
            priority=b.priority,
        ),
    )


def _adapter_kind(ctx: Context, job: sqlite3.Row) -> str:
    """The adapter kind the job was claimed for. A job claimed before schema v2
    recorded none and the migration had no inventory to backfill it from:
    resolve it from the inventory now (a pre-v2 job can only have used an
    adapter that existed then — never the local one), or say it is unrecorded."""
    if job["adapter"]:
        return str(job["adapter"])
    try:
        return ctx.inventory.get(str(job["provider"])).adapter
    except ConfigError:
        return "unrecorded (claimed before schema v2)"


INGEST_LEASE_S = 600.0
# Renewed on every scheduler iteration and before ingest, so it only has to
# outlast one iteration (a sleep up to the 30 min horizon + one provider call).
BATCH_LEASE_S = 3600.0


def ingest(ctx: Context, loaded: LoadedBrief, job: sqlite3.Row) -> bool:
    """Ingest one downloaded job: claim it (``downloaded`` -> ``ingesting``),
    record its images in the library and cut every target's variants, then mark
    it ``ingested`` — the rows and the final status in one transaction,
    conditional on the claim. Returns False when another process holds or has
    finished the job (nothing is written).

    A local I/O error (``OSError``) releases the claim and is re-raised: it is
    transient and the job is retried by the next run. Any other error is
    permanent for this job: it is marked ``failed`` with the reason."""
    token = uuid.uuid4().hex
    now = ctx.clock()
    claimed = ctx.conn.execute(
        "UPDATE jobs SET status = 'ingesting', claim = ?, next_attempt_at = ?, updated_at = ? "
        "WHERE id = ? AND status = 'downloaded' AND claim IS ?",
        (
            token,
            (now + timedelta(seconds=INGEST_LEASE_S)).isoformat(),
            now.isoformat(),
            job["id"],
            job["claim"],
        ),
    ).rowcount
    ctx.conn.commit()
    if claimed != 1:
        return False
    library = Library(ctx.conn)
    # Exactly what was sent and the terms as reviewed at submission (D5), as
    # persisted on the job when it was claimed.
    terms = TermsSnapshot.from_json(job["terms_json"])
    model = str(job["model"])
    prompt = str(job["prompt"])
    files = [Path(p) for p in json.loads(job["files_json"])]
    handle = SubmittedJob(
        external_id=str(job["external_id"] or ""), data=json.loads(job["handle_json"] or "{}")
    )
    brief = loaded.brief
    out_dir = ctx.workspace.variants_dir / job["batch_id"]
    created_at = ctx.clock().isoformat()
    adapter_kind = _adapter_kind(ctx, job)
    # Only the parameters the wire actually carried (none for a job claimed
    # before schema v2, which recorded none).
    sent: dict[str, Any] = json.loads(job["sent_json"] or "{}")

    def sidecar(
        path: Path,
        facade: dict[str, Any] | None,
        role: str,
        derived_from: str | None = None,
        transform: dict[str, Any] | None = None,
    ) -> None:
        provenance.write(
            path,
            provenance.build(
                file_sha256=sha256_file(path),
                file_name=path.name,
                role=role,
                provider_id=terms.provider,
                provider_name=terms.provider_name,
                adapter=adapter_kind,
                model=model,
                terms=asdict(terms),
                prompt=prompt,
                negative_prompt=sent.get("negative_prompt"),
                references=[
                    {"path": str(ref), "sha256": sha256_file(ref)} for ref in brief.reference_images
                ],
                seed=sent.get("seed"),
                recipe=sent.get("recipe"),
                created_at=created_at,
                job_id=str(job["id"]),
                batch_id=str(job["batch_id"]),
                brief_id=brief.id,
                facade=facade,
                derived_from=derived_from,
                transform=transform,
            ),
        )

    facades = staged_provenance(handle)
    try:
        # 1. All image work happens OUTSIDE any database transaction, so a slow
        #    encode never holds SQLite's write lock against other writers.
        planned: list[
            tuple[str, Path, list[tuple[str, str | None, str | None, str | None, str, Path]]]
        ] = []
        for n, raw in enumerate(files):
            uid = f"{job['id']}-{n}"
            facade = facades[n] if n < len(facades) else None
            raw_sha = sha256_file(raw)
            sidecar(raw, facade, "raw")
            with Image.open(raw) as opened:
                keep_alpha = opened.mode in ("RGBA", "LA", "P")
                source = opened.convert("RGBA" if keep_alpha else "RGB")
            variants: list[tuple[str, str | None, str | None, str | None, str, Path]] = []
            for target in brief.targets:
                if target.is_slot:
                    site = ctx.sites[target.site or ""]
                    slot = site.slots[target.slot or ""]
                    img = source
                    if slot.remove_background:
                        img = remove_background(img)
                    img = crop_to_slot(img, slot.width, slot.height, (slot.focal_x, slot.focal_y))
                    for fmt in slot.formats:
                        path = export(
                            img,
                            out_dir / f"{uid}--{site.id}--{target.slot}",
                            fmt,
                            ai_generated=True,
                        )
                        sidecar(
                            path,
                            facade,
                            "derivative",
                            derived_from=raw_sha,
                            transform={
                                "site": site.id,
                                "slot": target.slot,
                                "width": slot.width,
                                "height": slot.height,
                                "focal": [slot.focal_x, slot.focal_y],
                                "remove_background": slot.remove_background,
                                "format": fmt,
                            },
                        )
                        variants.append(
                            (
                                f"{uid}:{site.id}:{target.slot}:{fmt}",
                                site.id,
                                target.slot,
                                None,
                                fmt,
                                path,
                            )
                        )
                else:
                    path = export(
                        source,
                        out_dir / f"{uid}--campaign--{target.campaign}",
                        "webp",
                        ai_generated=True,
                    )
                    sidecar(
                        path,
                        facade,
                        "derivative",
                        derived_from=raw_sha,
                        transform={"campaign": target.campaign, "format": "webp"},
                    )
                    variants.append(
                        (
                            f"{uid}:campaign:{target.campaign}:webp",
                            None,
                            None,
                            target.campaign,
                            "webp",
                            path,
                        )
                    )
            planned.append((uid, raw, variants))

        # 2. One short write transaction: the rows and the final status.
        ctx.conn.commit()
        ctx.conn.execute("BEGIN IMMEDIATE")
        for uid, raw, variants in planned:
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
            for variant_id, site_id, slot_name, campaign, vfmt, vpath in variants:
                library.add_variant(
                    variant_id=variant_id,
                    asset_uid=asset.uid,
                    site=site_id,
                    slot=slot_name,
                    campaign=campaign,
                    fmt=vfmt,
                    path=vpath,
                    created_at=ctx.clock(),
                    commit=False,
                )
        done = ctx.conn.execute(
            "UPDATE jobs SET status = 'ingested', claim = NULL, updated_at = ? "
            "WHERE id = ? AND status = 'ingesting' AND claim = ?",
            (ctx.clock().isoformat(), job["id"], token),
        ).rowcount
        if done != 1:
            ctx.conn.rollback()
            return False
        ctx.conn.commit()
        return True
    except OSError:
        ctx.conn.rollback()
        ctx.conn.execute(
            "UPDATE jobs SET status = 'downloaded', claim = NULL, updated_at = ? "
            "WHERE id = ? AND status = 'ingesting' AND claim = ?",
            (ctx.clock().isoformat(), job["id"], token),
        )
        ctx.conn.commit()
        raise
    except Exception as e:
        ctx.conn.rollback()
        ctx.conn.execute(
            "UPDATE jobs SET status = 'failed', claim = NULL, last_error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'ingesting' AND claim = ?",
            (
                f"ingest failed: {type(e).__name__}: {e}",
                ctx.clock().isoformat(),
                job["id"],
                token,
            ),
        )
        ctx.conn.commit()
        return False
    except BaseException:
        ctx.conn.rollback()
        raise


class _Lease:
    """The one process driving a batch. Every status write while driving is
    conditional on holding it."""

    def __init__(self, ctx: Context, batch_id: str) -> None:
        self.ctx = ctx
        self.batch_id = batch_id
        self.token = uuid.uuid4().hex

    def acquire(self) -> bool:
        now = self.ctx.clock()
        cur = self.ctx.conn.execute(
            "UPDATE batches SET driver = ?, driver_until = ? "
            "WHERE id = ? AND status = 'generating' AND (driver IS NULL OR driver_until < ?)",
            (
                self.token,
                (now + timedelta(seconds=BATCH_LEASE_S)).isoformat(),
                self.batch_id,
                now.isoformat(),
            ),
        )
        self.ctx.conn.commit()
        return cur.rowcount == 1

    def renew(self) -> None:
        """Extend the lease from NOW. Raises :class:`LeaseLost` when it is no
        longer held (expired, or taken over) — the caller must stop driving."""
        now = self.ctx.clock()
        cur = self.ctx.conn.execute(
            "UPDATE batches SET driver_until = ? "
            "WHERE id = ? AND status = 'generating' AND driver = ? AND driver_until > ?",
            (
                (now + timedelta(seconds=BATCH_LEASE_S)).isoformat(),
                self.batch_id,
                self.token,
                now.isoformat(),
            ),
        )
        self.ctx.conn.commit()
        if cur.rowcount != 1:
            raise LeaseLost(f"lost the lease on batch {self.batch_id}")

    def set_status(self, status: str, reason: str | None = None) -> bool:
        """Set the batch status and release the lease, only if still held and
        unexpired."""
        now = self.ctx.clock()
        cur = self.ctx.conn.execute(
            "UPDATE batches SET status = ?, reason = ?, driver = NULL, driver_until = NULL "
            "WHERE id = ? AND status = 'generating' AND driver = ? AND driver_until > ?",
            (status, reason, self.batch_id, self.token, now.isoformat()),
        )
        self.ctx.conn.commit()
        return cur.rowcount == 1

    def release(self) -> None:
        self.ctx.conn.rollback()
        self.ctx.conn.execute(
            "UPDATE batches SET driver = NULL, driver_until = NULL WHERE id = ? AND driver = ?",
            (self.batch_id, self.token),
        )
        self.ctx.conn.commit()


def drive_batch(ctx: Context, batch_id: str, publish: bool = True) -> BatchOutcome:
    """Advance one ``generating`` batch: generate, ingest, then (gated) publish.

    Only one process drives a batch at a time (a lease on the batch row). A
    batch that is no longer ``generating``, or is being driven elsewhere, is
    returned untouched with ``driven=False``."""
    row = ctx.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ConfigError(f"no batch '{batch_id}'")
    if ctx.adapters.refusal is not None:
        # No image call may be made at all (gateway_has_no_image_route).
        raise ctx.adapters.refusal
    lease = _Lease(ctx, batch_id)
    if not lease.acquire():
        current = ctx.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        why = (
            "being driven by another process"
            if current["status"] == "generating"
            else f"is '{current['status']}', not generating"
        )
        return BatchOutcome(
            batch_id=batch_id, status=current["status"], driven=False, note=f"not driven: {why}"
        )
    try:
        outcome = _drive_leased(ctx, row, lease)
    finally:
        lease.release()
    if publish and outcome.status == "generated" and not row["no_publish"]:
        _publish_into(ctx, outcome)
    return outcome


def _publish_into(ctx: Context, outcome: BatchOutcome) -> None:
    """Publish a generated batch. A publish that could not even start (e.g. a
    site's leftover journal is invalid) leaves the batch 'generated' with the
    error recorded; the next `jobs run` retries it."""
    try:
        outcome.publish = publish_batch(
            ctx.conn,
            ctx.sites,
            outcome.batch_id,
            ctx.notifier,
            ctx.clock,
            bridge=ctx.bridge,
            fetch=ctx.fetch,
            root=ctx.workspace.root,
            sleep=ctx.sleep,
        )
    except StudioError as e:
        ctx.conn.rollback()
        outcome.error = f"publish did not start: {type(e).__name__}: {e}"
        return
    outcome.status = "published" if outcome.publish.published else "refused"


def _drive_leased(ctx: Context, row: sqlite3.Row, lease: _Lease) -> BatchOutcome:
    batch_id = row["id"]
    loaded = load_brief(Path(row["brief_path"]), root=ctx.workspace.root)
    if loaded.sha256 != row["brief_sha256"]:
        reason = (
            f"brief {row['brief_path']} changed since batch {batch_id} was created; "
            "run the brief again to start a new batch"
        )
        if lease.set_status("failed", reason):
            ctx.conn.execute(
                "UPDATE jobs SET status = 'failed', claim = NULL, last_error = ? "
                "WHERE batch_id = ? AND status IN ('queued', 'downloaded')",
                (reason, batch_id),
            )
            ctx.conn.commit()
        raise ConfigError(reason)
    outcome = BatchOutcome(batch_id=batch_id, status=row["status"])
    outcome.unavailable_providers = dict(ctx.adapters.unavailable)
    report = ctx.scheduler().run(
        {batch_id: _request_for(loaded)},
        sleep=ctx.sleep,
        on_iteration=lease.renew,
        on_wait=ctx.on_wait,
    )
    outcome.stopped_waiting = report.stopped_waiting
    lease.renew()

    # An ingest whose process died is reclaimable once its lease lapses.
    now = ctx.clock()
    ctx.conn.execute(
        "UPDATE jobs SET status = 'downloaded', claim = NULL "
        "WHERE batch_id = ? AND status = 'ingesting' AND next_attempt_at < ?",
        (batch_id, now.isoformat()),
    )
    ctx.conn.commit()
    for job in ctx.conn.execute(
        "SELECT * FROM jobs WHERE batch_id = ? AND status = 'downloaded' ORDER BY id", (batch_id,)
    ).fetchall():
        ingest(ctx, loaded, job)

    jobs = ctx.conn.execute(
        "SELECT id, status, last_error FROM jobs WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    outcome.completed_jobs = [j["id"] for j in jobs if j["status"] == "ingested"]
    outcome.failed_jobs = [j["id"] for j in jobs if j["status"] == "failed"]
    outcome.deferred_jobs = [
        j["id"]
        for j in jobs
        if j["status"] in ("queued", "submitting", "submitted", "downloaded", "ingesting")
    ]
    for jid in [*outcome.deferred_jobs, *outcome.failed_jobs]:
        last = ctx.conn.execute(
            "SELECT provider, outcome, detail FROM job_attempts WHERE job_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (jid,),
        ).fetchone()
        if last is not None:
            outcome.last_attempts[jid] = f"{last['outcome']} from {last['provider']}" + (
                f": {last['detail']}" if last["detail"] else ""
            )
    if outcome.deferred_jobs:
        outcome.status = "generating"
        return outcome
    if not outcome.completed_jobs:
        reasons = "; ".join(str(j["last_error"]) for j in jobs if j["last_error"])
        lease.set_status("failed", reasons or "every job failed")
        outcome.status = "failed"
        return outcome
    outcome.status = "generated" if lease.set_status("generated") else "generating"
    return outcome


def run_brief(ctx: Context, brief_path: Path, publish: bool = True) -> BatchOutcome:
    if ctx.adapters.refusal is not None:
        raise ctx.adapters.refusal
    loaded = load_brief(brief_path, root=ctx.workspace.root)
    problems = validate_brief(loaded.brief, ctx.sites, ctx.inventory)
    if problems:
        raise ConfigError(f"{brief_path}: " + "; ".join(problems))
    batch_id = new_batch(ctx, loaded, publish=publish)
    return drive_batch(ctx, batch_id, publish=publish)


def resume_all(ctx: Context, publish: bool = True) -> list[BatchOutcome]:
    """Recover interrupted publications, then resume every generating batch.

    Recovery runs per site; a site that cannot be recovered only holds back
    the batches that target it. One bad batch never stops the others."""
    report = recover(ctx.conn, ctx.sites, ctx.notifier, root=ctx.workspace.root)
    rows = ctx.conn.execute(
        "SELECT id, targets_json, status, no_publish FROM batches "
        "WHERE status = 'generating' OR (status = 'generated' AND no_publish = 0) "
        "ORDER BY created_at"
    ).fetchall()
    outcomes: list[BatchOutcome] = []
    for r in rows:
        targets = {str(site) for site, _ in json.loads(r["targets_json"])}
        blocked = sorted(targets & set(report.failed_sites))
        if blocked:
            outcomes.append(
                BatchOutcome(
                    batch_id=r["id"],
                    status="generating",
                    driven=False,
                    error="held back: recovery failed for site(s) "
                    + ", ".join(f"{s} ({report.failed_sites[s]})" for s in blocked),
                )
            )
            continue
        try:
            if r["status"] == "generated":
                # Generated but never published (crash, or a publish that could
                # not start): publish it now.
                if not publish:
                    continue
                stranded = BatchOutcome(batch_id=r["id"], status="generated")
                _publish_into(ctx, stranded)
                outcomes.append(stranded)
            else:
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
