"""Publication: run the D5/D6/D7 gates over a batch, publish it when they all
pass, notify the operator with a one-step unpublish, and unpublish on request.

Publishing a slot writes ``<publish_dir>/<slot>.<ext>`` for each exported
format and records the slot in ``<publish_dir>/studio-manifest.json`` with its
label, ``ai_generated: true`` and provenance — the manifest is what a site
build reads to render the illustrative/AI label beside the image (D6).

How a publish is made safe:

* **Exclusive.** Every publish/unpublish holds an OS lock on each affected
  site's publish dir AND preview dir (``locks.site_locks``) from the first
  check to the notice.
* **Never clobbers.** A slot path holding bytes that are not a live studio
  publication of that slot refuses the batch (gate ``foreign_file``) — checked
  when planning, again when the journal is written, and again immediately
  before each file is moved. A backup whose bytes were not a studio
  publication is never deleted.
* **D7 before anything goes live, with no waiver, bound to this batch.** The
  studio writes the live files plus this batch's files and merged manifest into
  ``preview_dir`` and confirms the preview serves exactly that manifest
  (sha256). It then drives the preview's UI Bridge to the batch's preview URL,
  confirms the bridged page is at that URL (its own ``page.url``), runs page
  health (OK/WARNING only), confirms the page did not move, and checks every
  changed slot's images loaded (``naturalWidth > 0``).
* **Journalled swap and unpublish.** Before touching live files, a journal is
  written to ``<publish_dir>/.studio-journal-<batch>.json``. The DB transaction
  (``BEGIN IMMEDIATE``, gate results included) is the commit point. Recovery
  replays a leftover journal — forward if the DB committed, backward otherwise —
  and notifies the operator. A journal is only acted on after validation: its
  batch must exist, and every path it names must be a slot file, staging file
  or backup file directly inside the site's own publish dir.
* **Unpublish re-gates what it restores.** The publication an unpublish would
  restore passes D5/D6 against the CURRENT site config and D7 on a preview of
  the post-unpublish state, or the slot is left empty and the notice says why.
* **Notified exactly when published.** ``batches.notified_at`` is set after
  the published notice is sent; recovery re-sends for a published batch that
  has none. Cleanup that fails after the commit is logged and left to recovery;
  it never turns a committed publication into a reported refusal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from qontinui_studio.commands import studio_command
from qontinui_studio.errors import ConfigError, JournalError, PublicationRefused, StudioError
from qontinui_studio.gates import (
    BridgeFactory,
    GateResult,
    HttpUiBridge,
    ManifestFetch,
    d5_licence,
    d5_provenance,
    d6_not_the_real_thing,
    d7_page_health,
    d7_preview_manifest,
    d7_preview_page,
    http_manifest_fetch,
)
from qontinui_studio.library import AssetRecord, Library, VariantRecord
from qontinui_studio.locks import site_locks
from qontinui_studio.notify import Delivery, Notice, Notifier
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.sites import Site

log = logging.getLogger(__name__)

MANIFEST = "studio-manifest.json"
JOURNAL_PREFIX = ".studio-journal-"
PUBLISHABLE_STATUSES = ("generated", "refused", "unpublished")
PREVIEW_FILES_KEY = "studio_preview_files"
SLOT_FILE_EXTS = ("webp", "avif", "png", "jpg")


class ForeignFile(StudioError):
    """A live slot path holds (or has come to hold) bytes the studio did not place."""

    def __init__(self, path: Path, why: str) -> None:
        super().__init__(f"{path}: {why}")
        self.path = path


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
    """The one-step unpublish, shell-quoted, runnable from any directory when
    ``root`` is given."""
    return studio_command(["unpublish", "--batch", batch_id], root)


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
    asset: AssetRecord,
    files: dict[str, str],
    shas: dict[str, str],
    label: str,
    batch_id: str,
    published_at: str,
) -> dict[str, Any]:
    """One slot's manifest entry. ``sha256`` (format -> digest) is what a site
    template appends as ``?sha=`` to each file URL: a cache-buster, and the
    proof D7 checks that the page shows THIS batch's bytes."""
    return {
        "files": files,
        "sha256": shas,
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


def _live_shas(conn: sqlite3.Connection, site_id: str, slot: str | None = None) -> set[str]:
    """sha256 of every live studio publication on a site (or one slot of it)."""
    sql = (
        "SELECT v.sha256 FROM publications p JOIN variants v ON v.id = p.variant_id "
        "WHERE p.site = ? AND p.unpublished_at IS NULL"
    )
    args: tuple[str, ...] = (site_id,)
    if slot is not None:
        sql += " AND p.slot = ?"
        args = (site_id, slot)
    return {r["sha256"] for r in conn.execute(sql, args)}


# ---- the journal ---------------------------------------------------------------

JournalKind = Literal["publish", "unpublish"]
_HEX32 = r"[0-9a-f]{32}"
_SHA = re.compile(r"[0-9a-f]{64}")


def _journal_path(publish_dir: Path, batch_id: str) -> Path:
    return publish_dir / f"{JOURNAL_PREFIX}{batch_id}.json"


@dataclass
class Moved:
    """A live file moved aside to a backup. ``sha`` is what the file held when
    it was planned; ``owned`` says whether that was a studio publication."""

    original: Path
    backup: Path
    sha: str
    owned: bool


@dataclass
class Journal:
    """Everything one publish or unpublish does to one site's live directory."""

    batch_id: str
    site: str
    publish_dir: Path
    kind: JournalKind = "publish"
    # (staged, dest, sha256 of the bytes placed at dest)
    placed: list[tuple[Path, Path, str]] = field(default_factory=list)
    moved: list[Moved] = field(default_factory=list)
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
            "placed": [[str(s), str(d), sha] for s, d, sha in self.placed],
            "moved": [[str(m.original), str(m.backup), m.sha, m.owned] for m in self.moved],
            "manifest_before": None
            if self.manifest_before is None
            else base64.b64encode(self.manifest_before).decode(),
            "manifest_after": base64.b64encode(self.manifest_after).decode(),
        }
        atomic_write(self.path, json.dumps(payload, indent=2).encode())

    @classmethod
    def read(cls, path: Path, site: Site) -> Journal:
        """Load and VALIDATE a journal found in ``site``'s publish dir. Any
        deviation raises :class:`JournalError`; nothing is acted on."""

        def bad(why: str) -> JournalError:
            return JournalError(f"site '{site.id}': refusing journal {path.name}: {why}")

        name = re.fullmatch(re.escape(JOURNAL_PREFIX) + r"(.+)\.json", path.name)
        if name is None:
            raise bad("not a journal file name")
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise bad(f"unreadable ({e})") from e
        if not isinstance(p, dict):
            raise bad("not a JSON object")
        if p.get("batch_id") != name.group(1):
            raise bad("batch_id does not match the file name")
        if p.get("site") != site.id:
            raise bad(f"names site {p.get('site')!r}")
        if p.get("kind") not in ("publish", "unpublish"):
            raise bad(f"unknown kind {p.get('kind')!r}")
        live = site.publish_dir.resolve()

        def slot_file(raw: object) -> Path:
            target = Path(str(raw))
            m = re.fullmatch(
                r"([a-z0-9][a-z0-9_-]*)\.(" + "|".join(SLOT_FILE_EXTS) + ")", target.name
            )
            if m is None or m.group(1) not in site.slots:
                raise bad(f"{target} is not a slot file of this site")
            if target.parent.resolve() != live or target != site.publish_dir / target.name:
                raise bad(f"{target} is not directly inside the site's publish dir")
            return site.publish_dir / target.name

        def sibling(raw: object, dest: Path, tag: str) -> Path:
            target = Path(str(raw))
            pattern = re.escape(f".{dest.name}.{tag}-") + _HEX32
            if not re.fullmatch(pattern, target.name) or target.parent.resolve() != live:
                raise bad(f"{target} is not a {tag} file of {dest.name} in the publish dir")
            return site.publish_dir / target.name

        def sha(raw: object) -> str:
            if not isinstance(raw, str) or not _SHA.fullmatch(raw):
                raise bad("bad sha256")
            return raw

        try:
            placed = []
            for staged, dest, digest in p["placed"]:
                d = slot_file(dest)
                placed.append((sibling(staged, d, "staged"), d, sha(digest)))
            moved = []
            for original, backup, digest, owned in p["moved"]:
                o = slot_file(original)
                moved.append(Moved(o, sibling(backup, o, "prev"), sha(digest), bool(owned)))
            before = p["manifest_before"]
            manifest_before = None if before is None else base64.b64decode(before, validate=True)
            manifest_after = base64.b64decode(p["manifest_after"], validate=True)
        except JournalError:
            raise
        except (KeyError, TypeError, ValueError, binascii.Error) as e:
            raise bad(f"malformed ({type(e).__name__}: {e})") from e
        return cls(
            batch_id=p["batch_id"],
            site=site.id,
            publish_dir=site.publish_dir,
            kind=p["kind"],
            placed=placed,
            moved=moved,
            manifest_before=manifest_before,
            manifest_after=manifest_after,
        )

    def apply(self) -> None:
        """Perform the journalled change. Each original is re-checked right
        before it moves: bytes that changed since planning abort the change."""
        for m in self.moved:
            if _file_sha(m.original) != m.sha:
                raise ForeignFile(m.original, "changed after it was checked; refusing to move it")
            os.replace(m.original, m.backup)
        for staged, dest, _ in self.placed:
            if dest.exists():
                raise ForeignFile(dest, "appeared after it was checked; refusing to overwrite it")
            os.replace(staged, dest)
        if self.manifest_after:
            atomic_write(self.publish_dir / MANIFEST, self.manifest_after)

    def roll_forward(self) -> list[str]:
        """Drop staging leftovers and backups. A backup that does not hold a
        studio publication's bytes is KEPT and reported, never deleted."""
        kept: list[str] = []
        for staged, _, _ in self.placed:
            staged.unlink(missing_ok=True)
        for m in self.moved:
            if not m.backup.exists():
                continue
            if m.owned and _file_sha(m.backup) == m.sha:
                m.backup.unlink()
            else:
                kept.append(f"kept {m.backup} (not a studio publication)")
        self.path.unlink(missing_ok=True)
        return kept

    def roll_back(self) -> list[str]:
        """Undo this change on disk. Returns what could NOT be restored."""
        problems: list[str] = []
        for staged, dest, digest in self.placed:
            # The journal is written only after every file is staged, so a
            # staged file that still exists was never renamed into place: the
            # live path is not ours to touch.
            if staged.exists():
                staged.unlink()
                continue
            # Placed: only remove the live file if it still holds OUR bytes.
            if _file_sha(dest) == digest:
                dest.unlink()
        for m in reversed(self.moved):
            if not m.backup.exists():
                continue
            if m.original.exists():
                problems.append(f"{m.original} holds unknown bytes; previous kept at {m.backup}")
                continue
            os.replace(m.backup, m.original)
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


def _finish(journal: Journal) -> None:
    """Post-commit cleanup. A failure here is logged and left for recovery —
    the change is already committed and must be reported as such."""
    try:
        for line in journal.roll_forward():
            log.warning("%s: %s", journal.site, line)
    except Exception as e:  # e.g. Windows refusing to unlink a file held open
        log.warning(
            "%s: post-commit cleanup of %s failed (%s: %s); the journal is left for recovery",
            journal.site,
            journal.batch_id,
            type(e).__name__,
            e,
        )


def _recover_locked(
    conn: sqlite3.Connection, sites: list[Site], notifier: Notifier, root: Path | None
) -> list[str]:
    """Replay every leftover journal in the given (already locked) sites.

    Raises :class:`JournalError` — acting on nothing — for a journal that does
    not validate or whose batch is unknown."""
    report: list[str] = []
    for site in sites:
        if not site.publish_dir.is_dir():
            continue
        for path in sorted(site.publish_dir.glob(f"{JOURNAL_PREFIX}*.json")):
            journal = Journal.read(path, site)
            if (
                conn.execute("SELECT 1 FROM batches WHERE id = ?", (journal.batch_id,)).fetchone()
                is None
            ):
                raise JournalError(
                    f"site '{site.id}': refusing journal {path.name}: unknown batch "
                    f"'{journal.batch_id}'"
                )
            if _journal_committed(conn, journal):
                kept = journal.roll_forward()
                report.append(
                    f"{site.id}: completed interrupted {journal.kind} {journal.batch_id}"
                    + (f" ({'; '.join(kept)})" if kept else "")
                )
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
    confined to the site or batch it concerns, and reported, never raised."""
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
            report.lines.append(f"{site.id}: recovery refused: {e}")

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
    gates = conn.execute("SELECT gate FROM gate_results WHERE batch_id = ?", (batch_id,)).fetchall()
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
        job = p[0].job_id or ""  # only generated assets have variants to publish
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


def _slot_gates(
    site: Site, slot_name: str, items: list[tuple[AssetRecord, VariantRecord]]
) -> list[GateResult]:
    """D5 provenance + licence and D6 for one slot's variants against the
    CURRENT site config."""
    slot = site.slots[slot_name]
    label = site.label_for_ai()
    out: list[GateResult] = []
    for asset, variant in items:
        out.append(d5_provenance(asset, variant))
        out.append(d5_licence(asset, variant, site))
        out.append(d6_not_the_real_thing(asset, variant, site, slot, label))
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
            {variant.format: variant.sha256 for _, variant, _ in rows},
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


def _write_preview(
    site: Site, overlay: dict[str, Path], drop: set[str], manifest: dict[str, Any]
) -> bytes:
    """Fill ``preview_dir`` with the site's live files minus ``drop``, overlaid
    by ``overlay`` (name -> source file), and ``manifest``. Returns the manifest
    bytes written. Only files the studio itself put there (listed in the
    previous preview manifest it wrote) are ever deleted."""
    preview = site.preview_dir
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
    if site.publish_dir.is_dir():
        for live in site.publish_dir.iterdir():
            if (
                live.is_file()
                and not live.name.startswith(".")
                and live.name != MANIFEST
                and live.name not in drop
                and live.name not in overlay
            ):
                atomic_copy(live, preview / live.name)
                names.add(live.name)
    for name, src in overlay.items():
        atomic_copy(src, preview / name)
        names.add(name)
    data = manifest_bytes({**manifest, PREVIEW_FILES_KEY: sorted(names)})
    atomic_write(previous, data)
    return data


def _d7(
    site: Site,
    label: str,
    overlay: dict[str, Path],
    drop: set[str],
    manifest: dict[str, Any],
    slot_files: dict[str, dict[str, str]],
    bridge: BridgeFactory,
    fetch: ManifestFetch,
    waiter: Waiter,
) -> list[GateResult]:
    """The full D7 for one site's would-be state: preview written and confirmed
    by manifest sha, then the bridged page bound to it and checked."""
    if not (site.ui_bridge_url and site.preview_dir):
        return [d7_page_health(site)]
    written = _write_preview(site, overlay, drop, manifest)
    preview_gate = d7_preview_manifest(site, _sha_bytes(written), fetch)
    if preview_gate.failed:
        return [preview_gate]
    return [
        preview_gate,
        *d7_preview_page(
            site,
            label,
            slot_files,
            bridge,
            fetch,
            sleep=waiter.sleep,
            monotonic=waiter.monotonic,
        ),
    ]


@dataclass(frozen=True)
class Waiter:
    """How D7 waits for the bridged page: ``sleep`` and a clock in seconds.
    Built from the caller's clock and sleep so tests run in fake time."""

    sleep: Callable[[float], None]
    monotonic: Callable[[], float]

    @classmethod
    def of(cls, clock: Callable[[], datetime], sleep: Callable[[float], None]) -> Waiter:
        return cls(sleep=sleep, monotonic=lambda: clock().timestamp())


def _lock_dirs(sites: list[Site]) -> list[Path]:
    dirs = [s.publish_dir for s in sites]
    dirs += [s.preview_dir for s in sites if s.preview_dir is not None]
    return dirs


# ---- publish -------------------------------------------------------------------


def publish_batch(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
    bridge: BridgeFactory = HttpUiBridge,
    fetch: ManifestFetch = http_manifest_fetch,
    root: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PublishResult:
    """Gate and publish one batch.

    Raises :class:`ConfigError` for an unknown batch, :class:`JournalError`
    for an invalid leftover journal in a target site, and
    :class:`PublicationRefused` for a batch whose status is not publishable.
    A gate refusal is not an exception: it returns ``published=False`` and
    sends a refusal notice. ``root`` (the studio root) is printed in the
    notice's unpublish command."""
    if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
        raise ConfigError(f"no batch '{batch_id}'")
    library = Library(conn)
    selected = _select_slot_variants(library.batch_variants(batch_id))
    locked = [sites[s] for s in sorted({s for s, _ in selected}) if s in sites]
    with site_locks(_lock_dirs(locked)):
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
        return _publish_locked(
            conn,
            sites,
            batch,
            selected,
            notifier,
            clock(),
            bridge,
            fetch,
            root,
            Waiter.of(clock, sleep),
        )


def _publish_locked(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch: sqlite3.Row,
    selected: dict[tuple[str, str], list[tuple[AssetRecord, VariantRecord]]],
    notifier: Notifier,
    now: datetime,
    bridge: BridgeFactory,
    fetch: ManifestFetch,
    root: Path | None,
    waiter: Waiter,
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
            result.gate_results += _slot_gates(site, slot_name, items)
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
        # ---- D7 on a preview bound to this batch, before anything live ------
        for plan in plans:
            overlay = {
                dest.name: Path(variant.path)
                for rows in plan.slots.values()
                for _, variant, dest in rows
            }
            slot_files = {
                slot: {dest.name: variant.sha256 for _, variant, dest in rows}
                for slot, rows in plan.slots.items()
            }
            # The preview must be exactly what the swap will produce: the
            # slot's currently live files that this batch does not overwrite
            # are moved away by the swap, so they are dropped here too.
            drop = {
                Path(r["published_path"]).name
                for slot in plan.slots
                for r in conn.execute(
                    "SELECT published_path FROM publications "
                    "WHERE site = ? AND slot = ? AND unpublished_at IS NULL",
                    (plan.site.id, slot),
                )
            } - set(overlay)
            result.gate_results += _d7(
                plan.site, batch_id, overlay, drop, plan.manifest, slot_files, bridge, fetch, waiter
            )
        if result.failures:
            return _refuse(conn, notifier, result, now, "D7 failed; nothing went live")

        _swap_live(conn, plans, batch_id, now, result)
    except ForeignFile as e:
        result.files = []
        result.gate_results.append(GateResult("foreign_file", str(e.path), "fail", str(e)))
        return _refuse(conn, notifier, result, now, "a slot file changed under the publish")
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
    """Journal, swap, commit (with the gate results). On any exception before
    the commit everything is rolled back and the exception re-raised. Cleanup
    after the commit never raises (see :func:`_finish`)."""
    now_iso = now.isoformat()
    journals: list[Journal] = []
    staged_files: list[Path] = []
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
            live_shas = _live_shas(conn, site.id)
            to_move: set[Path] = set()
            for slot_name, rows in plan.slots.items():
                for _, variant, dest in rows:
                    staged = dest.with_name(f".{dest.name}.staged-{uuid.uuid4().hex}")
                    staged_files.append(staged)
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
                sha_now = _file_sha(original)
                if sha_now is None:
                    continue
                if sha_now not in live_shas:
                    # Appeared (or changed) after the pre-publication check.
                    raise ForeignFile(original, "is not a live studio publication")
                journal.moved.append(
                    Moved(
                        original,
                        original.with_name(f".{original.name}.prev-{uuid.uuid4().hex}"),
                        sha_now,
                        True,
                    )
                )
            journal.write()
            journals.append(journal)

        # 2. swap (each original re-checked right before it moves).
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
        # Staged files not yet named by a written journal; the journalled
        # ones belong to roll_back (a staged file that exists = never placed).
        journalled = {staged for j in journals for staged, _, _ in j.placed}
        for staged in staged_files:
            if staged not in journalled:
                staged.unlink(missing_ok=True)
        raise
    # 4. committed: drop backups and journals; failures are left to recovery.
    for journal in journals:
        _finish(journal)


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
    gate_results: list[GateResult] = field(default_factory=list)


def _unpublish_locked(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    now: datetime,
    bridge: BridgeFactory,
    fetch: ManifestFetch,
    waiter: Waiter,
) -> UnpublishResult:
    """Journalled like a publish: decide everything (re-gating every restore),
    stage restore copies, write the journals, apply, then commit the DB."""
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
    restore_rows: list[tuple[int, str]] = []  # (publication id, current site label)
    staged_files: list[Path] = []
    try:
        for site_id, site_rows in sorted(by_site.items()):
            site = sites[site_id]
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
                sha_now = _file_sha(path)
                if sha_now is not None and sha_now == row["sha256"]:
                    journal.moved.append(
                        Moved(
                            path,
                            path.with_name(f".{path.name}.prev-{uuid.uuid4().hex}"),
                            sha_now,
                            True,
                        )
                    )
                    moving.add(path)
                    out.removed.append(path)
            # ---- candidate restores, each re-gated (D5/D6) ------------------
            restores: dict[str, tuple[str, list[sqlite3.Row]]] = {}
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
                if not problems and slot in site.slots:
                    items = [
                        (library.asset(r["asset_uid"]), library.variant(r["variant_id"]))
                        for r in prev
                    ]
                    gates = _slot_gates(site, slot, items)
                    out.gate_results += [_as_restore(g) for g in gates]
                    problems += [f"{g.gate}: {g.detail}" for g in gates if g.failed]
                elif slot not in site.slots:
                    problems.append("slot no longer defined")
                if problems:
                    out.not_restored.append(
                        f"{site_id}/{slot} <- {prev_batch}: slot left EMPTY ({'; '.join(problems)})"
                    )
                    continue
                restores[slot] = (prev_batch, prev)
                # The label is the CURRENT site's — the string D6 just checked —
                # never the one stored when the restored batch first went live.
                manifest["slots"][slot] = _manifest_entry(
                    library.asset(prev[0]["asset_uid"]),
                    {r["vformat"]: Path(r["published_path"]).name for r in prev},
                    {r["vformat"]: r["vsha"] for r in prev},
                    site.label_for_ai(),
                    prev_batch,
                    prev[0]["published_at"],
                )
            # ---- D7 on the post-unpublish state of this site ----------------
            if restores:
                overlay = {
                    Path(r["published_path"]).name: Path(r["vpath"])
                    for _, prev in restores.values()
                    for r in prev
                }
                slot_files = {
                    slot: {Path(r["published_path"]).name: r["vsha"] for r in prev}
                    for slot, (_, prev) in restores.items()
                }
                d7 = _d7(
                    site,
                    f"{batch_id}~unpublish",
                    overlay,
                    {p.name for p in moving},
                    manifest,
                    slot_files,
                    bridge,
                    fetch,
                    waiter,
                )
                out.gate_results += [_as_restore(g) for g in d7]
                failed = [f"{g.gate}: {g.detail}" for g in d7 if g.failed]
                if failed:
                    for slot, (prev_batch, _) in restores.items():
                        del manifest["slots"][slot]
                        out.not_restored.append(
                            f"{site_id}/{slot} <- {prev_batch}: slot left EMPTY (D7: "
                            f"{'; '.join(failed)})"
                        )
                    restores = {}
            for slot, (prev_batch, prev) in restores.items():
                for r in prev:
                    dest = Path(r["published_path"])
                    if dest in moving or _file_sha(dest) != r["vsha"]:
                        staged = dest.with_name(f".{dest.name}.staged-{uuid.uuid4().hex}")
                        staged_files.append(staged)
                        shutil.copyfile(r["vpath"], staged)
                        journal.placed.append((staged, dest, r["vsha"]))
                    restore_rows.append((int(r["id"]), site.label_for_ai()))
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
        for rid, label in restore_rows:
            conn.execute(
                "UPDATE publications SET unpublished_at = NULL, superseded_by = NULL, label = ? "
                "WHERE id = ?",
                (label, rid),
            )
        _record_gates(conn, batch_id, out.gate_results, now)
        conn.execute(
            "UPDATE batches SET status = 'unpublished', reason = NULL WHERE id = ?", (batch_id,)
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        for journal in reversed(journals):
            journal.roll_back()
        # Staged files not yet named by a written journal; the journalled
        # ones belong to roll_back (a staged file that exists = never placed).
        journalled = {staged for j in journals for staged, _, _ in j.placed}
        for staged in staged_files:
            if staged not in journalled:
                staged.unlink(missing_ok=True)
        raise
    for journal in journals:
        _finish(journal)
    return out


def _as_restore(g: GateResult) -> GateResult:
    return GateResult(g.gate, f"restore {g.subject}", g.outcome, g.detail)


def unpublish_batch(
    conn: sqlite3.Connection,
    sites: dict[str, Site],
    batch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
    root: Path | None = None,
    bridge: BridgeFactory = HttpUiBridge,
    fetch: ManifestFetch = http_manifest_fetch,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Path]:
    """Remove a batch's live files and manifest entries. On each slot, the
    publication it superseded is restored only if it passes D5/D6 against the
    current site config and D7 on a preview of the result; otherwise the slot
    is left empty and the notice says why. Returns the removed files.

    Raises ``KeyError`` for an unknown batch and :class:`PublicationRefused`
    for a batch that is not live, or whose live publication is on a site whose
    config is not loaded (its status is left unchanged)."""
    now = clock()
    if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
        raise KeyError(f"no batch '{batch_id}'")
    live_sites = {
        r["site"]
        for r in conn.execute(
            "SELECT DISTINCT site FROM publications WHERE batch_id = ? AND unpublished_at IS NULL",
            (batch_id,),
        )
    }
    missing = sorted(live_sites - set(sites))
    if missing:
        raise PublicationRefused(
            f"batch {batch_id} is live on site(s) {missing} whose config is not loaded "
            "(sites/*.yaml renamed or removed?); restore the site config, then unpublish",
            [],
        )
    site_ids = live_sites | {
        r["site"]
        for r in conn.execute(
            "SELECT DISTINCT site FROM publications WHERE batch_id = ?", (batch_id,)
        )
        if r["site"] in sites
    }
    locked = [sites[s] for s in sorted(site_ids)]
    with site_locks(_lock_dirs(locked)):
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
        out = _unpublish_locked(conn, sites, batch_id, now, bridge, fetch, Waiter.of(clock, sleep))
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
