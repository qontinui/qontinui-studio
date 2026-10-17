"""The asset library: generated and imported assets with D5 provenance, their
variants, their approval state, and the portable export of all of it."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from PIL import Image

from qontinui_studio.errors import ApprovalRefused, TagRefused
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.providers import Provider


@dataclass(frozen=True)
class TermsSnapshot:
    """A provider's terms AS REVIEWED, captured when a job is claimed for
    submission and persisted on the job (D5). Ingest records exactly this, so a
    later edit to providers.yaml never rewrites the licence an asset was made
    under."""

    provider: str
    provider_name: str
    commercial_use: str
    commercial_use_source: str | None
    tos_reviewed: str | None
    automation_allowed: str
    watermark: str
    watermark_visible: bool | None

    @classmethod
    def of(cls, provider: Provider) -> TermsSnapshot:
        return cls(
            provider=provider.id,
            provider_name=provider.provider,
            commercial_use=provider.commercial_use.value,
            commercial_use_source=provider.commercial_use.source,
            tos_reviewed=provider.tos_reviewed.isoformat() if provider.tos_reviewed else None,
            automation_allowed=provider.automation_allowed.value,
            watermark=provider.watermark,
            watermark_visible=provider.watermark_visible,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> TermsSnapshot:
        return cls(**json.loads(text))


# An operator alias (never an e-mail address): an account or a reviewer.
# Patterns end in ``(?![\s\S])`` (end of string) rather than ``$``: Python's
# ``$`` also matches before a trailing newline, ECMA-262's does not, and the
# same pattern strings appear in schemas/import-manifest.schema.json.
END_OF_STRING = r"(?![\s\S])"
ALIAS_PATTERN = r"^[A-Za-z0-9._-]+" + END_OF_STRING
APPROVAL_STATUSES = ("candidate", "approved", "rejected")
# A library tag names a set consumers select by (e.g. the brand reference set
# ``studio-style-v1``). Lowercase, so a set cannot be split by spelling.
TAG_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}" + END_OF_STRING
ORIGINS = ("generated", "imported")  # the CHECK on assets.origin (store.py)
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass(frozen=True)
class AssetRecord:
    """One library row. ``origin`` is 'generated' (made by a studio job:
    ``job_id``/``brief_id``/``provider_name``/``automation_allowed`` are set) or
    'imported' (``studio library import``: ``account``/``generated_on``/
    ``licence_terms_url`` are set and there is no job)."""

    uid: str
    asset_id: str
    brief_id: str | None
    batch_id: str
    job_id: str | None
    provider: str
    provider_name: str | None
    model: str
    prompt: str
    created_at: str
    raw_path: str
    sha256: str
    width: int
    height: int
    ai_generated: bool
    licence_commercial_use: str
    licence_source: str | None
    licence_tos_reviewed: str | None
    automation_allowed: str | None
    watermark: str
    watermark_visible: bool | None
    # v1 columns (store.py): defaults are what a generated asset carries.
    origin: str = "generated"
    mime: str | None = None
    licence_terms_url: str | None = None
    account: str | None = None
    reference_images_json: str = "[]"
    generated_on: str | None = None
    approval_status: str = "candidate"
    approval_by: str | None = None
    approval_on: str | None = None

    @property
    def reference_images(self) -> list[Any]:
        """Each item is an asset id in this library (str) or ``{url, licence}``."""
        return list(json.loads(self.reference_images_json))

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AssetRecord:
        wv = row["watermark_visible"]
        return cls(
            uid=row["uid"],
            asset_id=row["asset_id"],
            origin=row["origin"],
            brief_id=row["brief_id"],
            batch_id=row["batch_id"],
            job_id=row["job_id"],
            provider=row["provider"],
            provider_name=row["provider_name"],
            model=row["model"],
            prompt=row["prompt"],
            created_at=row["created_at"],
            raw_path=row["raw_path"],
            sha256=row["sha256"],
            width=row["width"],
            height=row["height"],
            mime=row["mime"],
            ai_generated=bool(row["ai_generated"]),
            licence_commercial_use=row["licence_commercial_use"],
            licence_source=row["licence_source"],
            licence_tos_reviewed=row["licence_tos_reviewed"],
            licence_terms_url=row["licence_terms_url"],
            automation_allowed=row["automation_allowed"],
            watermark=row["watermark"],
            watermark_visible=None if wv is None else bool(wv),
            account=row["account"],
            reference_images_json=row["reference_images_json"],
            generated_on=row["generated_on"],
            approval_status=row["approval_status"],
            approval_by=row["approval_by"],
            approval_on=row["approval_on"],
        )


@dataclass(frozen=True)
class VariantRecord:
    id: str
    asset_uid: str
    site: str | None
    slot: str | None
    campaign: str | None
    format: str
    path: str
    width: int
    height: int
    sha256: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> VariantRecord:
        return cls(
            id=row["id"],
            asset_uid=row["asset_uid"],
            site=row["site"],
            slot=row["slot"],
            campaign=row["campaign"],
            format=row["format"],
            path=row["path"],
            width=row["width"],
            height=row["height"],
            sha256=row["sha256"],
        )


class Library:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_asset(
        self,
        *,
        uid: str,
        asset_id: str,
        brief_id: str,
        batch_id: str,
        job_id: str,
        terms: TermsSnapshot,
        model: str,
        prompt: str,
        created_at: datetime,
        raw_path: Path,
        commit: bool = True,
    ) -> AssetRecord:
        """Record a generated asset with the terms snapshot taken at submission."""
        with Image.open(raw_path) as img:
            width, height = img.size
            mime = MIMES.get(img.format or "")
        self.conn.execute(
            "INSERT INTO assets (uid, asset_id, origin, brief_id, batch_id, job_id, provider, "
            "provider_name, model, prompt, created_at, raw_path, sha256, width, height, mime, "
            "ai_generated, licence_commercial_use, licence_source, licence_tos_reviewed, "
            "automation_allowed, watermark, watermark_visible) "
            "VALUES (?,?,'generated',?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
            (
                uid,
                asset_id,
                brief_id,
                batch_id,
                job_id,
                terms.provider,
                terms.provider_name,
                model,
                prompt,
                created_at.isoformat(),
                str(raw_path),
                sha256_file(raw_path),
                width,
                height,
                mime,
                terms.commercial_use,
                terms.commercial_use_source,
                terms.tos_reviewed,
                terms.automation_allowed,
                terms.watermark,
                None if terms.watermark_visible is None else int(terms.watermark_visible),
            ),
        )
        if commit:
            self.conn.commit()
        return self.asset(uid)

    def add_variant(
        self,
        *,
        variant_id: str,
        asset_uid: str,
        site: str | None,
        slot: str | None,
        campaign: str | None,
        fmt: str,
        path: Path,
        created_at: datetime,
        commit: bool = True,
    ) -> VariantRecord:
        with Image.open(path) as img:
            width, height = img.size
        self.conn.execute(
            "INSERT INTO variants (id, asset_uid, site, slot, campaign, format, path, width, height, "
            "sha256, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                variant_id,
                asset_uid,
                site,
                slot,
                campaign,
                fmt,
                str(path),
                width,
                height,
                sha256_file(path),
                created_at.isoformat(),
            ),
        )
        if commit:
            self.conn.commit()
        return VariantRecord.from_row(
            self.conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
        )

    def decide(self, uid: str, decision: str, by: str, on: date) -> tuple[AssetRecord, bool]:
        """Record an approval or rejection of one asset by ``by`` on ``on``.
        Returns the row and whether it changed (deciding what is already
        decided is a no-op). Approval is library metadata for consumers; it is
        not a publish gate — generated batches publish on the automated gates
        alone (D7).

        Refuses an unknown asset, a reviewer that is not an alias, and (D5)
        approving an asset whose licence is not ``commercial_use: yes``."""
        if decision not in ("approved", "rejected"):
            raise ApprovalRefused(f"unknown decision '{decision}'")
        if not is_alias(by):
            raise ApprovalRefused(
                f"reviewer '{by}' is not an alias (letters, digits, '.', '_', '-'; "
                "never an e-mail address)"
            )
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM assets WHERE uid = ?", (uid,)).fetchone()
            if row is None:
                raise ApprovalRefused(f"no asset '{uid}' in the library")
            current = AssetRecord.from_row(row)
            if decision == "approved" and current.licence_commercial_use != "yes":
                raise ApprovalRefused(
                    f"asset '{uid}' cannot be approved: licence_commercial_use is "
                    f"'{current.licence_commercial_use}', not 'yes' (D5)"
                )
            if current.approval_status == decision:
                self.conn.rollback()
                return current, False
            self.conn.execute(
                "UPDATE assets SET approval_status = ?, approval_by = ?, approval_on = ? "
                "WHERE uid = ?",
                (decision, by, on.isoformat(), uid),
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return self.asset(uid), True

    def tag(self, uid: str, tag: str, by: str, on: date, commit: bool = True) -> bool:
        """Put asset ``uid`` in the set ``tag``. ``by`` is a reviewer alias, or
        ``brief:<brief id>`` when a brief applied the tag at ingest. Returns
        whether a row was added (tagging twice is a no-op that keeps the first
        record). A tag is a selection, not an approval: it gates nothing."""
        if not is_tag(tag):
            raise TagRefused(
                f"tag '{tag}' must match {TAG_PATTERN} (lowercase letters, digits, '.', '_', '-')"
            )
        brief_id = by.removeprefix("brief:")
        if not is_alias(by) and not (by.startswith("brief:") and is_alias(brief_id)):
            raise TagRefused(f"tagger '{by}' is neither an alias nor 'brief:<brief id>'")
        if self.conn.execute("SELECT 1 FROM assets WHERE uid = ?", (uid,)).fetchone() is None:
            raise TagRefused(f"no asset '{uid}' in the library")
        added = self.conn.execute(
            "INSERT OR IGNORE INTO asset_tags (asset_uid, tag, tagged_by, tagged_on) "
            "VALUES (?, ?, ?, ?)",
            (uid, tag, by, on.isoformat()),
        ).rowcount
        if commit:
            self.conn.commit()
        return added == 1

    def untag(self, uid: str, tag: str) -> bool:
        """Take asset ``uid`` out of the set ``tag``; returns whether it was in it."""
        removed = self.conn.execute(
            "DELETE FROM asset_tags WHERE asset_uid = ? AND tag = ?", (uid, tag)
        ).rowcount
        self.conn.commit()
        return removed == 1

    def tags(self, uid: str) -> list[str]:
        return [
            r["tag"]
            for r in self.conn.execute(
                "SELECT tag FROM asset_tags WHERE asset_uid = ? ORDER BY tag", (uid,)
            )
        ]

    def tagged(self, tag: str, status: str | None = None) -> list[AssetRecord]:
        """Every asset in the set ``tag`` (optionally only those with approval
        ``status``), oldest first: the query a set's consumer runs."""
        sql = "SELECT a.* FROM assets a JOIN asset_tags t ON t.asset_uid = a.uid WHERE t.tag = ?"
        params: list[str] = [tag]
        if status is not None:
            sql += " AND a.approval_status = ?"
            params.append(status)
        rows = self.conn.execute(sql + " ORDER BY a.created_at, a.uid", params).fetchall()
        return [AssetRecord.from_row(r) for r in rows]

    def rejection(self, uid: str) -> str | None:
        """Why a consumer must not use asset ``uid``, or None. Only an explicit
        rejection counts: approval is library metadata, never a publish gate
        (D7), so a ``candidate`` asset stays usable. An unknown uid is None
        here; the D5 provenance gate is what refuses it."""
        row = self.conn.execute(
            "SELECT approval_status, approval_by, approval_on FROM assets WHERE uid = ?", (uid,)
        ).fetchone()
        if row is None or row["approval_status"] != "rejected":
            return None
        return (
            f"asset {uid} was rejected in the library by {row['approval_by']} "
            f"on {row['approval_on']}"
        )

    def asset(self, uid: str) -> AssetRecord:
        row = self.conn.execute("SELECT * FROM assets WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            raise KeyError(uid)
        return AssetRecord.from_row(row)

    def assets(self, batch_id: str | None = None) -> list[AssetRecord]:
        if batch_id is None:
            rows = self.conn.execute("SELECT * FROM assets ORDER BY created_at, uid").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM assets WHERE batch_id = ? ORDER BY uid", (batch_id,)
            ).fetchall()
        return [AssetRecord.from_row(r) for r in rows]

    def variant(self, variant_id: str) -> VariantRecord:
        row = self.conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
        if row is None:
            raise KeyError(variant_id)
        return VariantRecord.from_row(row)

    def variants(self, asset_uid: str) -> list[VariantRecord]:
        rows = self.conn.execute(
            "SELECT * FROM variants WHERE asset_uid = ? ORDER BY site, slot, format", (asset_uid,)
        ).fetchall()
        return [VariantRecord.from_row(r) for r in rows]

    def batch_variants(self, batch_id: str) -> list[tuple[AssetRecord, VariantRecord]]:
        out: list[tuple[AssetRecord, VariantRecord]] = []
        for asset in self.assets(batch_id):
            out.extend((asset, v) for v in self.variants(asset.uid))
        return out


def is_tag(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(TAG_PATTERN, value, re.ASCII) is not None


def is_alias(value: object) -> bool:
    """An operator alias: ``^[A-Za-z0-9._-]+$`` — so never an e-mail address
    (no '@') and never contains whitespace."""
    return isinstance(value, str) and re.fullmatch(ALIAS_PATTERN, value, re.ASCII) is not None


EXPORT_FORMAT = "qontinui-studio/library-export"
# Version 2 adds each asset's ``tags`` (schema v3).
EXPORT_VERSION = 2


def export_document(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every asset in the library as plain data: provenance, licence, approval,
    tags and variants, pointed at by SHA-256. No binaries and no local paths (the
    raw and variant files stay in ``.studio/``); ordered by (asset_id,
    created_at, uid) so the document is reproducible."""
    conn.commit()
    conn.execute("BEGIN")  # one consistent read
    try:
        rows = conn.execute("SELECT * FROM assets ORDER BY asset_id, created_at, uid").fetchall()
        library = Library(conn)
        assets: list[dict[str, Any]] = []
        for row in rows:
            a = AssetRecord.from_row(row)
            assets.append(
                {
                    "uid": a.uid,
                    "asset_id": a.asset_id,
                    "origin": a.origin,
                    "batch_id": a.batch_id,
                    "brief_id": a.brief_id,
                    "provider": a.provider,
                    "provider_name": a.provider_name,
                    "model": a.model,
                    "account": a.account,
                    "prompt": a.prompt,
                    "reference_images": a.reference_images,
                    "generated_on": a.generated_on,
                    "created_at": a.created_at,
                    "sha256": a.sha256,
                    "width": a.width,
                    "height": a.height,
                    "mime": a.mime,
                    "licence": {
                        "commercial_use": a.licence_commercial_use,
                        "tos_reviewed": a.licence_tos_reviewed,
                        "terms_url": a.licence_terms_url,
                        "source": a.licence_source,
                    },
                    "automation_allowed": a.automation_allowed,
                    "watermark": a.watermark,
                    "watermark_visible": a.watermark_visible,
                    "ai_generated": a.ai_generated,
                    "approval": {
                        "status": a.approval_status,
                        "by": a.approval_by,
                        "on": a.approval_on,
                    },
                    "tags": library.tags(a.uid),
                    "variants": [
                        {
                            "id": v.id,
                            "site": v.site,
                            "slot": v.slot,
                            "campaign": v.campaign,
                            "format": v.format,
                            "width": v.width,
                            "height": v.height,
                            "sha256": v.sha256,
                        }
                        for v in sorted(
                            library.variants(a.uid),
                            key=lambda v: (
                                v.site or "",
                                v.slot or "",
                                v.campaign or "",
                                v.format,
                                v.id,
                            ),
                        )
                    ],
                }
            )
    finally:
        conn.rollback()
    return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "assets": assets}


def export_yaml(conn: sqlite3.Connection) -> bytes:
    """:func:`export_document` as deterministic YAML (sorted keys): the same
    library always exports to the same bytes."""
    text = yaml.safe_dump(
        export_document(conn),
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=4096,
    )
    return text.encode("utf-8")
