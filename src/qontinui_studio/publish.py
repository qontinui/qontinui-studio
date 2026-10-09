"""Publication: run the D5/D6/D7 gates over a batch, publish it when they all
pass, notify the operator with a one-step unpublish, and unpublish on request.

Publishing a slot writes ``<publish_dir>/<slot>.<ext>`` for each exported
format and records the slot in ``<publish_dir>/studio-manifest.json`` with its
label, ``ai_generated: true`` and provenance — the manifest is what a site
build reads to render the illustrative/AI label beside the image (D6).

Publication is transactional at the file level:

1. every new file is first copied to a hidden staging name;
2. the slot's current files are moved aside to hidden backup names and the
   staged files are renamed into place (``os.replace``), manifests are written
   via temp file + ``os.replace``;
3. the D7 page-health probe then observes the NEW content (it has to — a probe
   of the old content would not test this batch);
4. if the probe fails, or any step above raised, the new files are removed,
   the backups renamed back, the manifests and the publication rows restored,
   so the previously live content is live again. Only on success are the
   backups deleted.

Unpublishing the newest batch on a slot restores the publication it superseded.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from qontinui_studio.errors import ConfigError, PublicationRefused
from qontinui_studio.gates import (
    GateResult,
    PageHealthProbe,
    d5_licence,
    d5_provenance,
    d6_not_the_real_thing,
    d7_page_health,
    http_page_health_probe,
)
from qontinui_studio.library import AssetRecord, Library, VariantRecord
from qontinui_studio.notify import Delivery, Notice, Notifier
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.sites import Site

MANIFEST = "studio-manifest.json"
PUBLISHABLE_STATUSES = ("generated", "refused", "unpublished")


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


def unpublish_command(batch_id: str) -> str:
    return f"studio unpublish --batch {batch_id}"


# ---- file helpers --------------------------------------------------------------


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex}")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def _read_manifest(publish_dir: Path) -> dict[str, Any]:
    path = publish_dir / MANIFEST
    if not path.is_file():
        return {"generated_by": "qontinui-studio", "slots": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"generated_by": "qontinui-studio", "slots": {}}
    data.setdefault("slots", {})
    return data


def _write_manifest(publish_dir: Path, data: dict[str, Any]) -> None:
    atomic_write(publish_dir / MANIFEST, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))


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


@dataclass
class _Swap:
    """What a publication changed on disk, so it can be undone exactly."""

    staged: list[Path] = field(default_factory=list)
    placed: list[Path] = field(default_factory=list)
    backups: list[tuple[Path, Path]] = field(default_factory=list)  # (original, backup)
    manifests: dict[Path, bytes | None] = field(default_factory=dict)

    def remember_manifest(self, publish_dir: Path) -> None:
        if publish_dir in self.manifests:
            return
        path = publish_dir / MANIFEST
        self.manifests[publish_dir] = path.read_bytes() if path.is_file() else None

    def restore(self) -> None:
        for p in self.staged + self.placed:
            p.unlink(missing_ok=True)
        for original, backup in reversed(self.backups):
            if backup.exists():
                os.replace(backup, original)
        for publish_dir, data in self.manifests.items():
            path = publish_dir / MANIFEST
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)

    def commit(self) -> None:
        for _, backup in self.backups:
            backup.unlink(missing_ok=True)


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


# ---- publish -------------------------------------------------------------------


def publish_batch(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
    probe: PageHealthProbe = http_page_health_probe,
) -> PublishResult:
    """Gate and publish one batch.

    Raises :class:`ConfigError` for an unknown batch and
    :class:`PublicationRefused` for a batch whose status is not publishable
    (still generating, failed, or already published). A gate refusal is not an
    exception: it returns ``published=False`` and sends a refusal notice."""
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ConfigError(f"no batch '{batch_id}'")
    if batch["status"] not in PUBLISHABLE_STATUSES:
        raise PublicationRefused(
            f"batch {batch_id} is '{batch['status']}'; only "
            f"{'/'.join(PUBLISHABLE_STATUSES)} batches can be published",
            [],
        )
    library = Library(conn)
    now = clock()
    result = PublishResult(batch_id=batch_id, published=False)
    selected = _select_slot_variants(library.batch_variants(batch_id))

    # ---- completeness: every brief target slot has a variant ----------------
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
            GateResult("completeness", f"batch {batch_id}", "fail", "brief targets no site slot")
        )

    # ---- pre-publication gates (D5, D6) -----------------------------------
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

    if result.failures:
        return _refuse(conn, notifier, result, now, "pre-publication gates failed")

    # ---- stage, then swap ------------------------------------------------
    swap = _Swap()
    inserted: list[int] = []
    touched_sites: dict[str, Site] = {}
    now_iso = now.isoformat()
    try:
        plan: list[tuple[Site, str, list[tuple[AssetRecord, VariantRecord, Path, Path]]]] = []
        for (site_id, slot_name), items in selected.items():
            site = sites[site_id]
            touched_sites[site_id] = site
            placements = []
            for asset, variant in items:
                src = Path(variant.path)
                dest = site.publish_dir / f"{slot_name}{src.suffix}"
                staged = dest.with_name(f".{dest.name}.staged-{uuid.uuid4().hex}")
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, staged)
                swap.staged.append(staged)
                placements.append((asset, variant, staged, dest))
            plan.append((site, slot_name, placements))

        for site, slot_name, placements in plan:
            previous = conn.execute(
                "SELECT published_path FROM publications "
                "WHERE site = ? AND slot = ? AND unpublished_at IS NULL",
                (site.id, slot_name),
            ).fetchall()
            to_move = {dest for _, _, _, dest in placements} | {
                Path(r["published_path"]) for r in previous
            }
            for original in sorted(to_move):
                if original.exists():
                    backup = original.with_name(f".{original.name}.prev-{uuid.uuid4().hex}")
                    os.replace(original, backup)
                    swap.backups.append((original, backup))
            for _, _, staged, dest in placements:
                os.replace(staged, dest)
                swap.staged.remove(staged)
                swap.placed.append(dest)
                result.files.append(dest)

        for site, slot_name, placements in plan:
            swap.remember_manifest(site.publish_dir)
            manifest = _read_manifest(site.publish_dir)
            files = {variant.format: dest.name for _, variant, _, dest in placements}
            manifest["slots"][slot_name] = _manifest_entry(
                placements[0][0], files, site.label_for_ai(), batch_id, now_iso
            )
            _write_manifest(site.publish_dir, manifest)

        for site, slot_name, placements in plan:
            conn.execute(
                "UPDATE publications SET unpublished_at = ?, superseded_by = ? "
                "WHERE site = ? AND slot = ? AND unpublished_at IS NULL",
                (now_iso, batch_id, site.id, slot_name),
            )
            for _, variant, _, dest in placements:
                cur = conn.execute(
                    "INSERT INTO publications (batch_id, variant_id, site, slot, published_path, "
                    "label, published_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        variant.id,
                        site.id,
                        slot_name,
                        str(dest),
                        site.label_for_ai(),
                        now_iso,
                    ),
                )
                inserted.append(int(cur.lastrowid or 0))
        conn.execute("UPDATE batches SET status = 'published' WHERE id = ?", (batch_id,))
        conn.commit()
    except Exception as e:  # any I/O or DB failure: put everything back
        conn.rollback()
        swap.restore()
        result.files = []
        result.gate_results.append(
            GateResult("publish_io", f"batch {batch_id}", "fail", f"{type(e).__name__}: {e}")
        )
        return _refuse(conn, notifier, result, now, "publishing failed; previous content restored")

    # ---- D7 page health over the NEW content; failure rolls back -----------
    for site in touched_sites.values():
        result.gate_results.append(d7_page_health(site, probe))
    if result.failures:
        swap.restore()
        _revert_rows(conn, batch_id, inserted, now_iso)
        result.files = []
        return _refuse(
            conn, notifier, result, now, "page-health gate failed; previous content restored"
        )
    swap.commit()

    _record_gates(conn, batch_id, result.gate_results, now)
    conn.commit()
    result.published = True

    urls = []
    for (site_id, slot_name), items in selected.items():
        site = sites[site_id]
        for _, variant in items:
            name = f"{slot_name}{Path(variant.path).suffix}"
            urls.append(
                f"{site.public_url.rstrip('/')}/{name}"
                if site.public_url
                else str(site.publish_dir / name)
            )
    body_lines = [
        f"{len(selected)} slot(s) across {len(touched_sites)} site(s) published; "
        "every D5/D6/D7 gate passed.",
    ]
    for g in result.gate_results:
        if g.outcome == "waived":
            body_lines.append(f"WAIVED: {g.gate} for {g.subject} — {g.detail}")
    result.delivery = notifier.notify(
        Notice(
            batch_id=batch_id,
            outcome="published",
            title=f"qontinui-studio published batch {batch_id}",
            body="\n".join(body_lines),
            urls=urls,
            unpublish_command=unpublish_command(batch_id),
        )
    )
    return result


def _revert_rows(
    conn: sqlite3.Connection, batch_id: str, inserted: list[int], now_iso: str
) -> None:
    conn.executemany("DELETE FROM publications WHERE id = ?", [(i,) for i in inserted])
    conn.execute(
        "UPDATE publications SET unpublished_at = NULL, superseded_by = NULL "
        "WHERE superseded_by = ? AND unpublished_at = ?",
        (batch_id, now_iso),
    )
    conn.commit()


def _refuse(
    conn: sqlite3.Connection, notifier: Notifier, result: PublishResult, now: datetime, why: str
) -> PublishResult:
    _record_gates(conn, result.batch_id, result.gate_results, now)
    conn.execute("UPDATE batches SET status = 'refused' WHERE id = ?", (result.batch_id,))
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


def _unpublish(
    conn: sqlite3.Connection, sites: dict[str, Site], batch_id: str, now: datetime
) -> UnpublishResult:
    out = UnpublishResult()
    now_iso = now.isoformat()
    library = Library(conn)
    rows = conn.execute(
        "SELECT p.id, p.site, p.slot, p.published_path, v.sha256 FROM publications p "
        "JOIN variants v ON v.id = p.variant_id WHERE p.batch_id = ? AND p.unpublished_at IS NULL",
        (batch_id,),
    ).fetchall()
    slots_by_site: dict[str, set[str]] = {}
    for row in rows:
        path = Path(row["published_path"])
        # Only delete the bytes we put there.
        if path.is_file() and sha256_file(path) == row["sha256"]:
            path.unlink()
            out.removed.append(path)
        slots_by_site.setdefault(row["site"], set()).add(row["slot"])
        conn.execute(
            "UPDATE publications SET unpublished_at = ? WHERE id = ?", (now_iso, row["id"])
        )

    # This batch's rows that a NEWER batch had already superseded: hand the
    # older publications this batch had superseded on to that newer batch, so
    # unpublishing the newer one later restores the right thing.
    for row in conn.execute(
        "SELECT DISTINCT site, slot, superseded_by FROM publications "
        "WHERE batch_id = ? AND superseded_by IS NOT NULL",
        (batch_id,),
    ).fetchall():
        conn.execute(
            "UPDATE publications SET superseded_by = ? WHERE superseded_by = ? AND site = ? AND slot = ?",
            (row["superseded_by"], batch_id, row["site"], row["slot"]),
        )
    conn.execute("UPDATE publications SET superseded_by = NULL WHERE batch_id = ?", (batch_id,))

    for site_id, slots in slots_by_site.items():
        site = sites.get(site_id)
        if site is None:
            continue
        manifest = _read_manifest(site.publish_dir)
        for slot in sorted(slots):
            entry = manifest["slots"].get(slot)
            if isinstance(entry, dict) and entry.get("batch_id") == batch_id:
                del manifest["slots"][slot]
            # Restore the publication this batch superseded on this slot.
            prev = conn.execute(
                "SELECT p.*, v.path AS vpath, v.format AS vformat, v.asset_uid FROM publications p "
                "JOIN variants v ON v.id = p.variant_id "
                "WHERE p.site = ? AND p.slot = ? AND p.superseded_by = ?",
                (site_id, slot, batch_id),
            ).fetchall()
            if not prev:
                continue
            sources = [Path(r["vpath"]) for r in prev]
            missing = [str(s) for s in sources if not s.is_file()]
            prev_batch = prev[0]["batch_id"]
            if missing:
                out.not_restored.append(f"{site_id}/{slot} <- {prev_batch}: missing {missing}")
                continue
            files: dict[str, str] = {}
            for r in prev:
                atomic_copy(Path(r["vpath"]), Path(r["published_path"]))
                files[r["vformat"]] = Path(r["published_path"]).name
                conn.execute(
                    "UPDATE publications SET unpublished_at = NULL, superseded_by = NULL WHERE id = ?",
                    (r["id"],),
                )
            manifest["slots"][slot] = _manifest_entry(
                library.asset(prev[0]["asset_uid"]),
                files,
                prev[0]["label"],
                prev_batch,
                prev[0]["published_at"],
            )
            out.restored.append(f"{site_id}/{slot} <- {prev_batch}")
        _write_manifest(site.publish_dir, manifest)
    conn.commit()
    return out


def unpublish_batch(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
) -> list[Path]:
    """Remove a batch's live files and manifest entries, restoring whatever
    publication it had superseded on each slot. Returns the removed files."""
    now = clock()
    if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
        raise KeyError(f"no batch '{batch_id}'")
    out = _unpublish(conn, sites, batch_id, now)
    conn.execute("UPDATE batches SET status = 'unpublished' WHERE id = ?", (batch_id,))
    conn.commit()
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
