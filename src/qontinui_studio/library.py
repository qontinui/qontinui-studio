"""The asset library: generated assets with D5 provenance, and their variants."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

from qontinui_studio.postprocess import sha256_file
from qontinui_studio.providers import Provider


@dataclass(frozen=True)
class AssetRecord:
    uid: str
    asset_id: str
    brief_id: str
    batch_id: str
    job_id: str
    provider: str
    provider_name: str
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
    automation_allowed: str
    watermark: str
    watermark_visible: bool | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AssetRecord:
        wv = row["watermark_visible"]
        return cls(
            uid=row["uid"],
            asset_id=row["asset_id"],
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
            ai_generated=bool(row["ai_generated"]),
            licence_commercial_use=row["licence_commercial_use"],
            licence_source=row["licence_source"],
            licence_tos_reviewed=row["licence_tos_reviewed"],
            automation_allowed=row["automation_allowed"],
            watermark=row["watermark"],
            watermark_visible=None if wv is None else bool(wv),
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
        provider: Provider,
        model: str,
        prompt: str,
        created_at: datetime,
        raw_path: Path,
    ) -> AssetRecord:
        """Record a generated asset, snapshotting the provider's terms AS REVIEWED
        at generation time — a later edit to providers.yaml does not rewrite the
        licence an asset was made under."""
        with Image.open(raw_path) as img:
            width, height = img.size
        self.conn.execute(
            "INSERT INTO assets (uid, asset_id, brief_id, batch_id, job_id, provider, provider_name, "
            "model, prompt, created_at, raw_path, sha256, width, height, ai_generated, "
            "licence_commercial_use, licence_source, licence_tos_reviewed, automation_allowed, "
            "watermark, watermark_visible) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
            (
                uid,
                asset_id,
                brief_id,
                batch_id,
                job_id,
                provider.id,
                provider.provider,
                model,
                prompt,
                created_at.isoformat(),
                str(raw_path),
                sha256_file(raw_path),
                width,
                height,
                provider.commercial_use.value,
                provider.commercial_use.source,
                provider.tos_reviewed.isoformat() if provider.tos_reviewed else None,
                provider.automation_allowed.value,
                provider.watermark,
                None if provider.watermark_visible is None else int(provider.watermark_visible),
            ),
        )
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
        self.conn.commit()
        return VariantRecord.from_row(
            self.conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
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
