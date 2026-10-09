"""Publication: run the D5/D6/D7 gates over a batch, publish it when they all
pass, notify the operator with a one-step unpublish, and unpublish on request.

Publishing a slot writes ``<publish_dir>/<slot>.<ext>`` for each exported
format and records the slot in ``<publish_dir>/studio-manifest.json`` with its
label, ``ai_generated: true`` and provenance — the manifest is what a site
build reads to render the illustrative/AI label beside the image (D6).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

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
    """Per (site, slot), the formats of the first generated variant (lowest job id).
    Later variants stay in the library for the review page."""
    chosen_asset: dict[tuple[str, str], str] = {}
    for asset, variant in sorted(pairs, key=lambda p: p[0].job_id):
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
    publish_dir.mkdir(parents=True, exist_ok=True)
    (publish_dir / MANIFEST).write_text(
        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
    )


def publish_batch(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
    probe: PageHealthProbe = http_page_health_probe,
) -> PublishResult:
    library = Library(conn)
    now = clock()
    result = PublishResult(batch_id=batch_id, published=False)
    selected = _select_slot_variants(library.batch_variants(batch_id))
    if not selected:
        result.gate_results.append(
            GateResult("publish", f"batch {batch_id}", "fail", "batch has no site-slot variants")
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

    # ---- publish ------------------------------------------------------------
    touched_sites: dict[str, Site] = {}
    for (site_id, slot_name), items in selected.items():
        site = sites[site_id]
        touched_sites[site_id] = site
        label = site.label_for_ai()
        # Supersede whatever this slot showed before.
        conn.execute(
            "UPDATE publications SET unpublished_at = ? "
            "WHERE site = ? AND slot = ? AND unpublished_at IS NULL",
            (now.isoformat(), site_id, slot_name),
        )
        manifest = _read_manifest(site.publish_dir)
        files: dict[str, str] = {}
        asset = items[0][0]
        for _, variant in items:
            src = Path(variant.path)
            dest = site.publish_dir / f"{slot_name}{src.suffix}"
            site.publish_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            files[variant.format] = dest.name
            result.files.append(dest)
            conn.execute(
                "INSERT INTO publications (batch_id, variant_id, site, slot, published_path, label, "
                "published_at) VALUES (?,?,?,?,?,?,?)",
                (batch_id, variant.id, site_id, slot_name, str(dest), label, now.isoformat()),
            )
        manifest["slots"][slot_name] = {
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
            "published_at": now.isoformat(),
        }
        _write_manifest(site.publish_dir, manifest)
    conn.commit()

    # ---- post-publication gate (D7 page health); failure rolls back --------
    for site in touched_sites.values():
        result.gate_results.append(d7_page_health(site, probe))
    if result.failures:
        _unpublish_files(conn, sites, batch_id, now)
        return _refuse(
            conn, notifier, result, now, "page-health gate failed; publication rolled back"
        )

    _record_gates(conn, batch_id, result.gate_results, now)
    conn.execute("UPDATE batches SET status = 'published' WHERE id = ?", (batch_id,))
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
    not_configured = [g for g in result.gate_results if g.outcome == "not_configured"]
    body_lines = [
        f"{len(selected)} slot(s) across {len(touched_sites)} site(s) published; every D5/D6 gate passed.",
    ]
    for g in not_configured:
        body_lines.append(f"NOT CONFIGURED: {g.gate} for {g.subject} — {g.detail}")
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


def _unpublish_files(
    conn: sqlite3.Connection, sites: dict[str, Site], batch_id: str, now: datetime
) -> list[Path]:
    removed: list[Path] = []
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
            removed.append(path)
        slots_by_site.setdefault(row["site"], set()).add(row["slot"])
        conn.execute(
            "UPDATE publications SET unpublished_at = ? WHERE id = ?", (now.isoformat(), row["id"])
        )
    for site_id, slots in slots_by_site.items():
        site = sites.get(site_id)
        if site is None:
            continue
        manifest = _read_manifest(site.publish_dir)
        for slot in slots:
            entry = manifest["slots"].get(slot)
            if isinstance(entry, dict) and entry.get("batch_id") == batch_id:
                del manifest["slots"][slot]
        _write_manifest(site.publish_dir, manifest)
    conn.commit()
    return removed


def unpublish_batch(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
) -> list[Path]:
    now = clock()
    if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
        raise KeyError(f"no batch '{batch_id}'")
    removed = _unpublish_files(conn, sites, batch_id, now)
    conn.execute("UPDATE batches SET status = 'unpublished' WHERE id = ?", (batch_id,))
    conn.commit()
    notifier.notify(
        Notice(
            batch_id=batch_id,
            outcome="unpublished",
            title=f"qontinui-studio unpublished batch {batch_id}",
            body=f"Removed {len(removed)} file(s): " + ", ".join(str(p) for p in removed),
        )
    )
    return removed
