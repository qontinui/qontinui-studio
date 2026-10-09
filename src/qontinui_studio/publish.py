"""Publication: run the D5/D6/D7 gates over a batch, publish it when they all
pass, notify the operator with a one-step unpublish, and unpublish on request.

Publishing a slot writes ``<publish_dir>/<slot>.<ext>`` for each exported
format and records the slot in ``<publish_dir>/studio-manifest.json`` with its
label, ``ai_generated: true`` and provenance — the manifest is what a site
build reads to render the illustrative/AI label beside the image (D6).

How a publish is made safe:

* **Exclusive.** Every publish/unpublish holds an OS lock on each affected
  site (``locks.site_locks``) from the first check to the notice.
* **Never clobbers.** A slot path holding bytes that are not a live studio
  publication of that slot (an owner-supplied photo, a hand edit) refuses the
  batch (gate ``foreign_file``). A batch already live is refused (``already_live``).
* **D7 before anything goes live, with no waiver.** The studio writes the live
  files plus this batch's files and merged manifest into ``preview_dir``,
  confirms the preview serves exactly that manifest (sha256), then runs UI
  Bridge page health on it. Only OK/WARNING pass. A site with no page-health
  preview cannot be published to.
* **Journalled swap and unpublish.** Before touching live files, a journal
  naming every staged, placed and backup path (and the manifest before/after)
  is written to ``<publish_dir>/.studio-journal-<batch>.json``. The DB
  transaction (``BEGIN IMMEDIATE``, gate results included) is the commit
  point. A crash anywhere leaves a journal that recovery replays — forward if
  the DB committed, backward otherwise — and the operator is notified either
  way. Undo only removes a live file that still holds the bytes it placed.
* **Notified exactly when published.** ``batches.notified_at`` is set after
  the published notice is sent, under the site locks; recovery re-sends for a
  published batch that has none.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from qontinui_studio.errors import ConfigError, PublicationRefused
from qontinui_studio.gates import (
    GateResult,
    ManifestFetch,
    PageHealthProbe,
    d5_licence,
    d5_provenance,
    d6_not_the_real_thing,
    d7_page_health,
    d7_preview_manifest,
    http_manifest_fetch,
    http_page_health_probe,
)
from qontinui_studio.library import AssetRecord, Library, VariantRecord
from qontinui_studio.locks import site_locks
from qontinui_studio.notify import Delivery, Notice, Notifier
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.sites import Site

MANIFEST = "studio-manifest.json"
JOURNAL_PREFIX = ".studio-journal-"
PUBLISHABLE_STATUSES = ("generated", "refused", "unpublished")
PREVIEW_FILES_KEY = "studio_preview_files"


@dataclass
class PublishResult:
    batch_id: str
    published: bool
    gate_results: list[GateResult] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    delivery: Delivery | None = None

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gate_results if g.failed]


def unpublish_command(batch_id: str, root: Path | None = None) -> str:
    """The one-step unpublish, runnable from any directory when ``root`` is given."""
    prefix = f"studio --root {root.resolve()} " if root is not None else "studio "
    return f"{prefix}unpublish --batch {batch_id}"


# ---- file helpers --------------------------------------------------------------


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex}")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def _read_manifest(publish_dir: Path) -> dict[str, Any]:
    path = publish_dir / MANIFEST
    if not path.is_file():
        return {"generated_by": "qontinui-studio", "slots": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"generated_by": "qontinui-studio", "slots": {}}
    data.setdefault("slots", {})
    return data


def manifest_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True).encode("utf-8")


def _write_manifest(publish_dir: Path, data: dict[str, Any]) -> None:
    atomic_write(publish_dir / MANIFEST, manifest_bytes(data))


def _manifest_entry(
    asset: AssetRecord, files: dict[str, str], label: str, batch_id: str, published_at: str
) -> dict[str, Any]:
    return {
        "files": files,
        "label": label,
        "ai_generated": True,
        "asset_uid": asset.uid,
        "batch_id": batch_id,
        "provider": asset.provider,
        "model": asset.model,
        "licence": {
            "commercial_use": asset.licence_commercial_use,
            "tos_reviewed": asset.licence_tos_reviewed,
            "source": asset.licence_source,
        },
        "watermark": asset.watermark,
        "published_at": published_at,
    }


def _live_shas(conn: sqlite3.Connection, site_id: str, slot: str) -> set[str]:
    return {
        r["sha256"]
        for r in conn.execute(
            "SELECT v.sha256 FROM publications p JOIN variants v ON v.id = p.variant_id "
            "WHERE p.site = ? AND p.slot = ? AND p.unpublished_at IS NULL",
            (site_id, slot),
        )
    }


# ---- the journal ---------------------------------------------------------------

JournalKind = Literal["publish", "unpublish"]


def _journal_path(publish_dir: Path, batch_id: str) -> Path:
    return publish_dir / f"{JOURNAL_PREFIX}{batch_id}.json"


@dataclass
class Journal:
    """Everything one publish or unpublish does to one site's live directory."""

    batch_id: str
    site: str
    publish_dir: Path
    kind: JournalKind = "publish"
    # (staged, dest, sha256 of the bytes placed at dest)
    placed: list[tuple[Path, Path, str]] = field(default_factory=list)
    # (original live path, backup path it is moved to)
    moved: list[tuple[Path, Path]] = field(default_factory=list)
    manifest_before: bytes | None = None
    manifest_after: bytes = b""

    @property
    def path(self) -> Path:
        return _journal_path(self.publish_dir, self.batch_id)

    def write(self) -> None:
        payload = {
            "kind": self.kind,
            "batch_id": self.batch_id,
            "site": self.site,
            "publish_dir": str(self.publish_dir),
            "placed": [[str(s), str(d), sha] for s, d, sha in self.placed],
            "moved": [[str(o), str(b)] for o, b in self.moved],
            "manifest_before": None
            if self.manifest_before is None
            else base64.b64encode(self.manifest_before).decode(),
            "manifest_after": base64.b64encode(self.manifest_after).decode(),
        }
        atomic_write(self.path, json.dumps(payload, indent=2).encode())

    @classmethod
    def read(cls, path: Path) -> Journal:
        p = json.loads(path.read_text(encoding="utf-8"))
        before = p["manifest_before"]
        return cls(
            batch_id=p["batch_id"],
            site=p["site"],
            publish_dir=Path(p["publish_dir"]),
            kind=p.get("kind", "publish"),
            placed=[(Path(s), Path(d), sha) for s, d, sha in p["placed"]],
            moved=[(Path(o), Path(b)) for o, b in p["moved"]],
            manifest_before=None if before is None else base64.b64decode(before),
            manifest_after=base64.b64decode(p["manifest_after"]),
        )

    def apply(self) -> None:
        """Perform the journalled change: move originals aside, rename staged
        files into place, write the manifest."""
        for original, backup in self.moved:
            os.replace(original, backup)
        for staged, dest, _ in self.placed:
            os.replace(staged, dest)
        manifest = self.publish_dir / MANIFEST
        if self.manifest_after:
            atomic_write(manifest, self.manifest_after)

    def roll_forward(self) -> None:
        for staged, _, _ in self.placed:
            staged.unlink(missing_ok=True)
        for _, backup in self.moved:
            backup.unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)

    def roll_back(self) -> list[str]:
        """Undo this change on disk. Returns what could NOT be restored."""
        problems: list[str] = []
        for staged, dest, sha in self.placed:
            # The journal is written only after every file is staged, so a
            # staged file that still exists was never renamed into place: the
            # live path is not ours to touch.
            if staged.exists():
                staged.unlink()
                continue
            # Placed: only remove the live file if it still holds OUR bytes.
            if _file_sha(dest) == sha:
                dest.unlink()
        for original, backup in reversed(self.moved):
            if not backup.exists():
                continue
            if original.exists():
                problems.append(f"{original} holds unknown bytes; previous kept at {backup}")
                continue
            os.replace(backup, original)
        manifest = self.publish_dir / MANIFEST
        current = manifest.read_bytes() if manifest.is_file() else None
        if current is not None and current == self.manifest_after:
            if self.manifest_before is None:
                manifest.unlink()
            else:
                atomic_write(manifest, self.manifest_before)
        elif current != self.manifest_before:
            problems.append(f"{manifest} changed by someone else; left as is")
        self.path.unlink(missing_ok=True)
        return problems


def _journal_committed(conn: sqlite3.Connection, journal: Journal) -> bool:
    row = conn.execute("SELECT status FROM batches WHERE id = ?", (journal.batch_id,)).fetchone()
    if row is None:
        return False
    live = (
        conn.execute(
            "SELECT 1 FROM publications WHERE batch_id = ? AND site = ? AND unpublished_at IS NULL",
            (journal.batch_id, journal.site),
        ).fetchone()
        is not None
    )
    if journal.kind == "publish":
        return bool(row["status"] == "published" and live)
    return bool(row["status"] == "unpublished" and not live)


def _recover_locked(
    conn: sqlite3.Connection, sites: list[Site], notifier: Notifier, root: Path | None
) -> list[str]:
    """Replay every leftover journal in the given (already locked) sites. A
    rolled-back change is reported to the operator."""
    report: list[str] = []
    for site in sites:
        if not site.publish_dir.is_dir():
            continue
        for path in sorted(site.publish_dir.glob(f"{JOURNAL_PREFIX}*.json")):
            journal = Journal.read(path)
            if _journal_committed(conn, journal):
                journal.roll_forward()
                report.append(f"{site.id}: completed interrupted {journal.kind} {journal.batch_id}")
                continue
            problems = journal.roll_back()
            if journal.kind == "publish":
                conn.execute(
                    "UPDATE batches SET status = 'refused', reason = ? "
                    "WHERE id = ? AND status = 'publishing'",
                    ("interrupted publish rolled back", journal.batch_id),
                )
                conn.commit()
            line = f"{site.id}: rolled back interrupted {journal.kind} {journal.batch_id}" + (
                f" ({'; '.join(problems)})" if problems else ""
            )
            report.append(line)
            notifier.notify(
                Notice(
                    batch_id=journal.batch_id,
                    outcome=f"{journal.kind}-rolled-back",
                    title=f"qontinui-studio rolled back an interrupted {journal.kind} of "
                    f"{journal.batch_id} on {site.id}",
                    body=line,
                    unpublish_command=None,
                )
            )
    return report


@dataclass
class RecoveryReport:
    lines: list[str] = field(default_factory=list)
    # site id -> why its recovery failed; batches targeting it are held back.
    failed_sites: dict[str, str] = field(default_factory=dict)


def recover(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    notifier: Notifier,
    root: Path | None = None,
) -> RecoveryReport:
    """Bring every site back to a consistent state after a crash.

    Per site (each under its own lock, each isolated): replay leftover
    journals. Then refuse orphaned ``publishing`` batches and send the
    published notice for any published batch that never got one. A failure is
    confined to the site or batch it concerns."""
    report = RecoveryReport()
    for site in sites.values():
        if not site.publish_dir.is_dir():
            continue  # nothing published there, so nothing to recover
        try:
            with site_locks([site.publish_dir]):
                report.lines += _recover_locked(conn, [site], notifier, root)
        except Exception as e:
            conn.rollback()
            report.failed_sites[site.id] = f"{type(e).__name__}: {e}"

    for row in conn.execute(
        "SELECT id, targets_json FROM batches WHERE status = 'publishing'"
    ).fetchall():
        targets = {str(s) for s, _ in json.loads(row["targets_json"])}
        if targets & set(report.failed_sites):
            continue
        try:
            dirs = [sites[s].publish_dir for s in targets if s in sites]
            with site_locks([d for d in dirs if d.is_dir()]):
                if any(_journal_path(d, row["id"]).exists() for d in dirs):
                    continue
                cur = conn.execute(
                    "UPDATE batches SET status = 'refused', reason = ? "
                    "WHERE id = ? AND status = 'publishing'",
                    ("publish interrupted before any live change", row["id"]),
                )
                conn.commit()
                if cur.rowcount == 1:
                    report.lines.append(f"batch {row['id']}: interrupted publish marked refused")
                    notifier.notify(
                        Notice(
                            batch_id=row["id"],
                            outcome="refused",
                            title=f"qontinui-studio: publish of {row['id']} was interrupted",
                            body="The publish was interrupted before any live change; "
                            "the batch is refused and can be published again.",
                        )
                    )
        except Exception as e:
            conn.rollback()
            report.lines.append(f"batch {row['id']}: recovery failed: {type(e).__name__}: {e}")

    for row in conn.execute(
        "SELECT id FROM batches WHERE status = 'published' AND notified_at IS NULL"
    ).fetchall():
        try:
            site_ids = {
                r["site"]
                for r in conn.execute(
                    "SELECT DISTINCT site FROM publications WHERE batch_id = ?", (row["id"],)
                )
            }
            if site_ids & set(report.failed_sites):
                continue
            with site_locks([sites[s].publish_dir for s in site_ids if s in sites]):
                again = conn.execute(
                    "SELECT status, notified_at FROM batches WHERE id = ?", (row["id"],)
                ).fetchone()
                if again["status"] != "published" or again["notified_at"] is not None:
                    continue
                notifier.notify(_published_notice(conn, sites, row["id"], root))
                _mark_notified(conn, row["id"])
                report.lines.append(f"batch {row['id']}: sent the missing published notice")
        except Exception as e:
            conn.rollback()
            report.lines.append(f"batch {row['id']}: notice failed: {type(e).__name__}: {e}")
    return report


def _mark_notified(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute(
        "UPDATE batches SET notified_at = datetime('now') WHERE id = ? AND notified_at IS NULL",
        (batch_id,),
    )
    conn.commit()


def _published_notice(
    conn: sqlite3.Connection, sites: dict[str, Site], batch_id: str, root: Path | None
) -> Notice:
    rows = conn.execute(
        "SELECT site, slot, published_path FROM publications "
        "WHERE batch_id = ? AND unpublished_at IS NULL ORDER BY site, slot, published_path",
        (batch_id,),
    ).fetchall()
    urls = []
    for r in rows:
        site = sites.get(r["site"])
        name = Path(r["published_path"]).name
        urls.append(
            f"{site.public_url.rstrip('/')}/{name}"
            if site and site.public_url
            else r["published_path"]
        )
    slots = {(r["site"], r["slot"]) for r in rows}
    gates = conn.execute(
        "SELECT gate, outcome FROM gate_results WHERE batch_id = ? ORDER BY rowid DESC",
        (batch_id,),
    ).fetchall()
    return Notice(
        batch_id=batch_id,
        outcome="published",
        title=f"qontinui-studio published batch {batch_id}",
        body=f"{len(slots)} slot(s) across {len({s for s, _ in slots})} site(s) published; "
        f"every D5/D6/D7 gate passed ({len(gates)} gate results recorded).",
        urls=urls,
        unpublish_command=unpublish_command(batch_id, root),
    )


# ---- gates ---------------------------------------------------------------------


def _record_gates(
    conn: sqlite3.Connection, batch_id: str, results: list[GateResult], now: datetime
) -> None:
    conn.executemany(
        "INSERT INTO gate_results (batch_id, gate, subject, outcome, detail, evaluated_at) "
        "VALUES (?,?,?,?,?,?)",
        [(batch_id, g.gate, g.subject, g.outcome, g.detail, now.isoformat()) for g in results],
    )


def _select_slot_variants(
    pairs: list[tuple[AssetRecord, VariantRecord]],
) -> dict[tuple[str, str], list[tuple[AssetRecord, VariantRecord]]]:
    """Per (site, slot), the formats of the first generated variant (lowest
    variant index). Later variants stay in the library for the review page."""

    def order(p: tuple[AssetRecord, VariantRecord]) -> tuple[int, str]:
        job = p[0].job_id
        idx = job.rsplit("-v", 1)[-1]
        return (int(idx) if idx.isdigit() else 0, p[0].uid)

    chosen_asset: dict[tuple[str, str], str] = {}
    for asset, variant in sorted(pairs, key=order):
        if variant.site is None or variant.slot is None:
            continue
        chosen_asset.setdefault((variant.site, variant.slot), asset.uid)
    out: dict[tuple[str, str], list[tuple[AssetRecord, VariantRecord]]] = {}
    for asset, variant in pairs:
        if variant.site is None or variant.slot is None:
            continue
        key = (variant.site, variant.slot)
        if chosen_asset.get(key) == asset.uid:
            out.setdefault(key, []).append((asset, variant))
    return out


@dataclass
class _SitePlan:
    site: Site
    # slot -> [(asset, variant, dest)]
    slots: dict[str, list[tuple[AssetRecord, VariantRecord, Path]]]
    manifest: dict[str, Any]

    @property
    def manifest_bytes(self) -> bytes:
        return manifest_bytes(self.manifest)


def _plan(
    selected: dict[tuple[str, str], list[tuple[AssetRecord, VariantRecord]]],
    sites: dict[str, Site],
    batch_id: str,
    now_iso: str,
) -> list[_SitePlan]:
    plans: dict[str, _SitePlan] = {}
    for (site_id, slot_name), items in sorted(selected.items()):
        site = sites[site_id]
        plan = plans.get(site_id)
        if plan is None:
            plan = plans[site_id] = _SitePlan(site, {}, _read_manifest(site.publish_dir))
        rows = [
            (asset, variant, site.publish_dir / f"{slot_name}{Path(variant.path).suffix}")
            for asset, variant in items
        ]
        plan.slots[slot_name] = rows
        plan.manifest["slots"][slot_name] = _manifest_entry(
            rows[0][0],
            {variant.format: dest.name for _, variant, dest in rows},
            site.label_for_ai(),
            batch_id,
            now_iso,
        )
    return list(plans.values())


def _safe_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and "/" not in name
        and "\\" not in name
        and name not in (".", "..")
    )


def _write_preview(plan: _SitePlan) -> bytes:
    """Fill ``preview_dir`` with the live files overlaid by this batch's files
    and the merged manifest. Returns the manifest bytes written.

    Only files the studio itself put there (listed in the previous preview
    manifest it wrote) are ever deleted."""
    preview = plan.site.preview_dir
    assert preview is not None
    preview.mkdir(parents=True, exist_ok=True)
    previous = preview / MANIFEST
    if previous.is_file():
        try:
            old = json.loads(previous.read_text(encoding="utf-8"))
        except ValueError:
            old = None
        if isinstance(old, dict) and old.get("generated_by") == "qontinui-studio":
            for name in old.get(PREVIEW_FILES_KEY, []):
                if _safe_name(name):
                    (preview / name).unlink(missing_ok=True)
    names: set[str] = set()
    if plan.site.publish_dir.is_dir():
        for live in plan.site.publish_dir.iterdir():
            if live.is_file() and not live.name.startswith(".") and live.name != MANIFEST:
                atomic_copy(live, preview / live.name)
                names.add(live.name)
    for rows in plan.slots.values():
        for _, variant, dest in rows:
            atomic_copy(Path(variant.path), preview / dest.name)
            names.add(dest.name)
    data = manifest_bytes({**plan.manifest, PREVIEW_FILES_KEY: sorted(names)})
    atomic_write(previous, data)
    return data


# ---- publish -------------------------------------------------------------------


def publish_batch(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
    probe: PageHealthProbe = http_page_health_probe,
    fetch: ManifestFetch = http_manifest_fetch,
    root: Path | None = None,
) -> PublishResult:
    """Gate and publish one batch.

    Raises :class:`ConfigError` for an unknown batch and
    :class:`PublicationRefused` for a batch whose status is not publishable
    (still generating, failed, publishing or already published). A gate
    refusal is not an exception: it returns ``published=False`` and sends a
    refusal notice. ``root`` is the studio root, printed in the notice's
    unpublish command."""
    if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
        raise ConfigError(f"no batch '{batch_id}'")
    library = Library(conn)
    selected = _select_slot_variants(library.batch_variants(batch_id))
    locked = [sites[s] for s in {s for s, _ in selected} if s in sites]
    with site_locks([s.publish_dir for s in locked]):
        _recover_locked(conn, locked, notifier, root)
        batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if batch["status"] not in PUBLISHABLE_STATUSES:
            raise PublicationRefused(
                f"batch {batch_id} is '{batch['status']}'; only "
                f"{'/'.join(PUBLISHABLE_STATUSES)} batches can be published",
                [],
            )
        claimed = conn.execute(
            "UPDATE batches SET status = 'publishing', reason = NULL, notified_at = NULL "
            "WHERE id = ? AND status = ?",
            (batch_id, batch["status"]),
        ).rowcount
        conn.commit()
        if claimed != 1:
            raise PublicationRefused(f"batch {batch_id} changed status concurrently", [])
        return _publish_locked(conn, sites, batch, selected, notifier, clock(), probe, fetch, root)


def _publish_locked(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch: sqlite3.Row,
    selected: dict[tuple[str, str], list[tuple[AssetRecord, VariantRecord]]],
    notifier: Notifier,
    now: datetime,
    probe: PageHealthProbe,
    fetch: ManifestFetch,
    root: Path | None,
) -> PublishResult:
    batch_id = batch["id"]
    result = PublishResult(batch_id=batch_id, published=False)
    now_iso = now.isoformat()
    try:
        # ---- completeness: every brief target slot has a variant ------------
        required = {(str(site), str(slot)) for site, slot in json.loads(batch["targets_json"])}
        for site_id, slot_name in sorted(required - set(selected)):
            result.gate_results.append(
                GateResult(
                    "completeness",
                    f"{site_id}/{slot_name}",
                    "fail",
                    "the brief targets this slot but the batch has no variant for it",
                )
            )
        if not required:
            result.gate_results.append(
                GateResult(
                    "completeness", f"batch {batch_id}", "fail", "brief targets no site slot"
                )
            )
        if conn.execute(
            "SELECT 1 FROM publications WHERE batch_id = ? AND unpublished_at IS NULL", (batch_id,)
        ).fetchone():
            result.gate_results.append(
                GateResult("already_live", f"batch {batch_id}", "fail", "batch is already live")
            )

        # ---- pre-publication gates (D5, D6, foreign files) ------------------
        for (site_id, slot_name), items in selected.items():
            site = sites.get(site_id)
            if site is None or slot_name not in site.slots:
                result.gate_results.append(
                    GateResult(
                        "config", f"{site_id}/{slot_name}", "fail", "site or slot no longer defined"
                    )
                )
                continue
            slot = site.slots[slot_name]
            label = site.label_for_ai()
            for asset, variant in items:
                result.gate_results.append(d5_provenance(asset, variant))
                result.gate_results.append(d5_licence(asset, variant, site))
                result.gate_results.append(d6_not_the_real_thing(asset, variant, site, slot, label))
            live = _live_shas(conn, site_id, slot_name)
            dests = {site.publish_dir / f"{slot_name}{Path(v.path).suffix}" for _, v in items}
            prev_paths = {
                Path(r["published_path"])
                for r in conn.execute(
                    "SELECT published_path FROM publications "
                    "WHERE site = ? AND slot = ? AND unpublished_at IS NULL",
                    (site_id, slot_name),
                )
            }
            for path in sorted(dests | prev_paths):
                sha = _file_sha(path)
                if sha is not None and sha not in live:
                    result.gate_results.append(
                        GateResult(
                            "foreign_file",
                            str(path),
                            "fail",
                            "holds bytes that are not a live studio publication of this slot "
                            "(owner-supplied or hand-edited); refusing to overwrite",
                        )
                    )
        if result.failures:
            return _refuse(conn, notifier, result, now, "pre-publication gates failed")

        plans = _plan(selected, sites, batch_id, now_iso)
        # ---- D7 on a preview, before anything live changes ------------------
        for plan in plans:
            site = plan.site
            if site.page_health_url:
                written = _write_preview(plan)
                preview_gate = d7_preview_manifest(site, _sha_bytes(written), fetch)
                result.gate_results.append(preview_gate)
                if preview_gate.failed:
                    continue
            result.gate_results.append(d7_page_health(site, probe))
        if result.failures:
            return _refuse(conn, notifier, result, now, "D7 failed; nothing went live")

        _swap_live(conn, plans, batch_id, now, result)
    except Exception as e:
        result.files = []
        result.gate_results.append(
            GateResult("publish_io", f"batch {batch_id}", "fail", f"{type(e).__name__}: {e}")
        )
        return _refuse(conn, notifier, result, now, "publishing failed; previous content restored")
    except BaseException:
        # Interrupted (e.g. Ctrl-C): never leave 'publishing' behind.
        conn.rollback()
        conn.execute(
            "UPDATE batches SET status = 'refused', reason = 'interrupted' "
            "WHERE id = ? AND status = 'publishing'",
            (batch_id,),
        )
        conn.commit()
        raise

    result.published = True
    result.delivery = notifier.notify(_published_notice(conn, sites, batch_id, root))
    _mark_notified(conn, batch_id)
    return result


def _swap_live(
    conn: sqlite3.Connection,
    plans: list[_SitePlan],
    batch_id: str,
    now: datetime,
    result: PublishResult,
) -> None:
    """Journal, swap, commit (with the gate results). On any exception
    everything is rolled back and the exception re-raised."""
    now_iso = now.isoformat()
    journals: list[Journal] = []
    try:
        # 1. stage every file and write every journal before any live change.
        for plan in plans:
            site = plan.site
            site.publish_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = site.publish_dir / MANIFEST
            journal = Journal(
                batch_id=batch_id,
                site=site.id,
                publish_dir=site.publish_dir,
                kind="publish",
                manifest_before=manifest_path.read_bytes() if manifest_path.is_file() else None,
                manifest_after=plan.manifest_bytes,
            )
            to_move: set[Path] = set()
            for slot_name, rows in plan.slots.items():
                for _, variant, dest in rows:
                    staged = dest.with_name(f".{dest.name}.staged-{uuid.uuid4().hex}")
                    shutil.copyfile(variant.path, staged)
                    sha = sha256_file(staged)
                    if sha != variant.sha256:
                        raise OSError(f"staged copy of {variant.path} does not match its sha256")
                    journal.placed.append((staged, dest, sha))
                    to_move.add(dest)
                for prev in conn.execute(
                    "SELECT published_path FROM publications "
                    "WHERE site = ? AND slot = ? AND unpublished_at IS NULL",
                    (site.id, slot_name),
                ).fetchall():
                    to_move.add(Path(prev["published_path"]))
            for original in sorted(to_move):
                if original.exists():
                    journal.moved.append(
                        (original, original.with_name(f".{original.name}.prev-{uuid.uuid4().hex}"))
                    )
            journal.write()
            journals.append(journal)

        # 2. swap.
        for journal in journals:
            journal.apply()
            result.files += [dest for _, dest, _ in journal.placed]

        # 3. commit point — publications, status and the gate record together.
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for plan in plans:
            for slot_name, rows in plan.slots.items():
                conn.execute(
                    "UPDATE publications SET unpublished_at = ?, superseded_by = ? "
                    "WHERE site = ? AND slot = ? AND unpublished_at IS NULL",
                    (now_iso, batch_id, plan.site.id, slot_name),
                )
                for _, variant, dest in rows:
                    conn.execute(
                        "INSERT INTO publications (batch_id, variant_id, site, slot, "
                        "published_path, label, published_at) VALUES (?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            variant.id,
                            plan.site.id,
                            slot_name,
                            str(dest),
                            plan.site.label_for_ai(),
                            now_iso,
                        ),
                    )
        _record_gates(conn, batch_id, result.gate_results, now)
        cur = conn.execute(
            "UPDATE batches SET status = 'published', reason = NULL "
            "WHERE id = ? AND status = 'publishing'",
            (batch_id,),
        )
        if cur.rowcount != 1:
            raise PublicationRefused(f"batch {batch_id} left 'publishing' concurrently", [])
        conn.commit()
    except BaseException:
        conn.rollback()
        for journal in reversed(journals):
            journal.roll_back()
        raise
    # 4. committed: drop backups and journals.
    for journal in journals:
        journal.roll_forward()


def _refuse(
    conn: sqlite3.Connection, notifier: Notifier, result: PublishResult, now: datetime, why: str
) -> PublishResult:
    conn.rollback()
    _record_gates(conn, result.batch_id, result.gate_results, now)
    conn.execute(
        "UPDATE batches SET status = 'refused', reason = ? WHERE id = ? AND status = 'publishing'",
        (why, result.batch_id),
    )
    conn.commit()
    result.files = []
    lines = [why + ":"] + [f"- {g.gate} {g.subject}: {g.detail}" for g in result.failures]
    result.delivery = notifier.notify(
        Notice(
            batch_id=result.batch_id,
            outcome="refused",
            title=f"qontinui-studio refused batch {result.batch_id}",
            body="\n".join(lines),
        )
    )
    return result


# ---- unpublish -----------------------------------------------------------------


@dataclass
class UnpublishResult:
    removed: list[Path] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)  # "site/slot <- batch"
    not_restored: list[str] = field(default_factory=list)


def _unpublish_locked(
    conn: sqlite3.Connection, sites: dict[str, Site], batch_id: str, now: datetime
) -> UnpublishResult:
    """Journalled like a publish: decide everything, stage restore copies,
    write the journals, apply (move this batch's files aside, rename restores
    into place, write manifests), then commit the DB. Recovery replays it."""
    out = UnpublishResult()
    now_iso = now.isoformat()
    library = Library(conn)
    rows = conn.execute(
        "SELECT p.id, p.site, p.slot, p.published_path, v.sha256 FROM publications p "
        "JOIN variants v ON v.id = p.variant_id WHERE p.batch_id = ? AND p.unpublished_at IS NULL",
        (batch_id,),
    ).fetchall()
    by_site: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_site.setdefault(row["site"], []).append(row)

    journals: list[Journal] = []
    restore_rows: list[int] = []
    try:
        for site_id, site_rows in sorted(by_site.items()):
            site = sites.get(site_id)
            if site is None:
                continue
            manifest_path = site.publish_dir / MANIFEST
            manifest = _read_manifest(site.publish_dir)
            journal = Journal(
                batch_id=batch_id,
                site=site_id,
                publish_dir=site.publish_dir,
                kind="unpublish",
                manifest_before=manifest_path.read_bytes() if manifest_path.is_file() else None,
            )
            moving: set[Path] = set()
            for row in site_rows:
                path = Path(row["published_path"])
                # Only move away the bytes we put there.
                if _file_sha(path) == row["sha256"]:
                    journal.moved.append(
                        (path, path.with_name(f".{path.name}.prev-{uuid.uuid4().hex}"))
                    )
                    moving.add(path)
                    out.removed.append(path)
            for slot in sorted({r["slot"] for r in site_rows}):
                entry = manifest["slots"].get(slot)
                if isinstance(entry, dict) and entry.get("batch_id") == batch_id:
                    del manifest["slots"][slot]
                prev = conn.execute(
                    "SELECT p.*, v.path AS vpath, v.format AS vformat, v.sha256 AS vsha, "
                    "v.asset_uid FROM publications p JOIN variants v ON v.id = p.variant_id "
                    "WHERE p.site = ? AND p.slot = ? AND p.superseded_by = ?",
                    (site_id, slot, batch_id),
                ).fetchall()
                if not prev:
                    continue
                prev_batch = prev[0]["batch_id"]
                problems: list[str] = []
                for r in prev:
                    src, dest = Path(r["vpath"]), Path(r["published_path"])
                    if _file_sha(src) != r["vsha"]:
                        problems.append(f"stored variant {src} is missing or altered")
                    existing = None if dest in moving else _file_sha(dest)
                    if existing is not None and existing != r["vsha"]:
                        problems.append(f"{dest} holds bytes the studio did not place")
                if problems:
                    out.not_restored.append(
                        f"{site_id}/{slot} <- {prev_batch}: {'; '.join(problems)}"
                    )
                    continue
                files: dict[str, str] = {}
                for r in prev:
                    dest = Path(r["published_path"])
                    if dest in moving or _file_sha(dest) != r["vsha"]:
                        staged = dest.with_name(f".{dest.name}.staged-{uuid.uuid4().hex}")
                        shutil.copyfile(r["vpath"], staged)
                        journal.placed.append((staged, dest, r["vsha"]))
                    files[r["vformat"]] = dest.name
                    restore_rows.append(int(r["id"]))
                manifest["slots"][slot] = _manifest_entry(
                    library.asset(prev[0]["asset_uid"]),
                    files,
                    prev[0]["label"],
                    prev_batch,
                    prev[0]["published_at"],
                )
                out.restored.append(f"{site_id}/{slot} <- {prev_batch}")
            journal.manifest_after = manifest_bytes(manifest)
            journal.write()
            journals.append(journal)

        for journal in journals:
            journal.apply()

        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            conn.execute(
                "UPDATE publications SET unpublished_at = ? WHERE id = ?", (now_iso, row["id"])
            )
        # This batch's rows that a NEWER batch had already superseded: hand the
        # publications this batch superseded on to that newer batch, so
        # unpublishing the newer one later restores the right thing.
        for r in conn.execute(
            "SELECT DISTINCT site, slot, superseded_by FROM publications "
            "WHERE batch_id = ? AND superseded_by IS NOT NULL",
            (batch_id,),
        ).fetchall():
            conn.execute(
                "UPDATE publications SET superseded_by = ? "
                "WHERE superseded_by = ? AND site = ? AND slot = ?",
                (r["superseded_by"], batch_id, r["site"], r["slot"]),
            )
        conn.execute("UPDATE publications SET superseded_by = NULL WHERE batch_id = ?", (batch_id,))
        for rid in restore_rows:
            conn.execute(
                "UPDATE publications SET unpublished_at = NULL, superseded_by = NULL WHERE id = ?",
                (rid,),
            )
        conn.execute(
            "UPDATE batches SET status = 'unpublished', reason = NULL WHERE id = ?", (batch_id,)
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        for journal in reversed(journals):
            journal.roll_back()
        raise
    for journal in journals:
        journal.roll_forward()
    return out


def unpublish_batch(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
    root: Path | None = None,
) -> list[Path]:
    """Remove a batch's live files and manifest entries, restoring whatever
    publication it had superseded on each slot. Returns the removed files.

    Raises ``KeyError`` for an unknown batch and :class:`PublicationRefused`
    for a batch that is neither ``published`` nor has live publication rows
    (its status is left unchanged)."""
    now = clock()
    if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
        raise KeyError(f"no batch '{batch_id}'")
    site_ids = {
        r["site"]
        for r in conn.execute(
            "SELECT DISTINCT site FROM publications WHERE batch_id = ?", (batch_id,)
        )
    }
    locked = [sites[s] for s in site_ids if s in sites]
    with site_locks([s.publish_dir for s in locked]):
        _recover_locked(conn, locked, notifier, root)
        status = conn.execute("SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()[
            "status"
        ]
        live = conn.execute(
            "SELECT 1 FROM publications WHERE batch_id = ? AND unpublished_at IS NULL", (batch_id,)
        ).fetchone()
        if status != "published" and live is None:
            raise PublicationRefused(
                f"batch {batch_id} is '{status}' and has no live publication; nothing to unpublish",
                [],
            )
        out = _unpublish_locked(conn, sites, batch_id, now)
        lines = [f"Removed {len(out.removed)} file(s): " + ", ".join(str(p) for p in out.removed)]
        lines += [f"Restored {r}" for r in out.restored]
        lines += [f"NOT restored {r}" for r in out.not_restored]
        notifier.notify(
            Notice(
                batch_id=batch_id,
                outcome="unpublished",
                title=f"qontinui-studio unpublished batch {batch_id}",
                body="\n".join(lines),
            )
        )
    return out.removed
