"""Decorative assets for a launch, through the Phase 2 path unchanged:
brief -> scheduler -> library -> D5/D6/D7 gates -> the assets site's publish dir.

The kit only READS what that path published (``studio-manifest.json`` on the
product's ``assets_site``), so every decorative asset in a bundle has already
passed the Phase 2 gates and carries its provenance and AI label. None is
required (D1: decoration never stands in for the record): every failure here
is a note in the bundle, never a failed launch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import features

from qontinui_studio.errors import StudioError
from qontinui_studio.launch.product import LaunchProduct
from qontinui_studio.library import Library
from qontinui_studio.pipeline import Context, run_brief
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.publish import MANIFEST
from qontinui_studio.sites import Site

# Formats the bundle's renderers (Pillow, ffmpeg) read everywhere, best first.
_PREFERRED = ("png", "jpeg", "webp", "avif")


@dataclass(frozen=True)
class DecorAsset:
    slot: str
    path: Path
    label: str
    asset_uid: str
    batch_id: str
    provider: str
    model: str


@dataclass
class DecorOutcome:
    assets: list[DecorAsset] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_slot(self, slot: str) -> DecorAsset | None:
        return next((a for a in self.assets if a.slot == slot), None)

    def broll(self) -> list[DecorAsset]:
        return [a for a in self.assets if a.slot.startswith("broll")]


def generate(ctx: Context, product: LaunchProduct) -> list[str]:
    """Run the product's brief through the Phase 2 pipeline; return notes."""
    if product.brief is None:
        return ["no brief configured; the bundle carries no decorative assets"]
    try:
        outcome = run_brief(ctx, product.brief)
    except StudioError as e:
        return [f"decorative assets not generated: {e}"]
    notes = [f"brief {product.brief.name}: batch {outcome.batch_id} {outcome.status}"]
    if outcome.publish is not None:
        notes += [f"gate {g.gate} {g.outcome}: {g.detail}" for g in outcome.publish.failures]
    return notes


def collect(
    product: LaunchProduct, sites: dict[str, Site], library: Library | None = None
) -> DecorOutcome:
    """The assets the Phase 2 gates published on the product's assets site.
    With a ``library``, an asset the operator rejected (``studio library
    reject``) is skipped with a note: publication happened on the automated
    gates (D7), but a rejected asset is one consumers must not use."""
    out = DecorOutcome()
    if product.assets_site is None:
        out.notes.append("no assets_site configured")
        return out
    site = sites.get(product.assets_site)
    if site is None:
        out.notes.append(f"assets_site '{product.assets_site}' is not defined under sites/")
        return out
    manifest_path = site.publish_dir / MANIFEST
    if not manifest_path.is_file():
        out.notes.append(f"nothing published on assets site '{site.id}' yet")
        return out
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out.notes.append(f"unreadable {manifest_path}: {e}")
        return out
    slots = manifest.get("slots") if isinstance(manifest, dict) else None
    if not isinstance(slots, dict):
        out.notes.append(f"{manifest_path} has no slots")
        return out
    for slot, entry in sorted(slots.items()):
        if not isinstance(entry, dict):
            continue
        files = entry.get("files") or {}
        label = str(entry.get("label") or "")
        if entry.get("ai_generated") is not True or not label.strip():
            out.notes.append(f"slot {slot}: skipped, its manifest entry lacks ai_generated/label")
            continue
        shas = entry.get("sha256") or {}
        chosen = None
        for fmt in _PREFERRED:
            if fmt == "avif" and not features.check("avif"):
                continue
            name = files.get(fmt)
            path = site.publish_dir / name if isinstance(name, str) else None
            # Only the bytes the manifest says the pipeline published.
            if path is not None and path.is_file() and sha256_file(path) == shas.get(fmt):
                chosen = path
                break
        if chosen is None:
            out.notes.append(
                f"slot {slot}: no published file matching its manifest sha256 (skipped)"
            )
            continue
        uid = str(entry.get("asset_uid") or "")
        rejected = library.rejection(uid) if library is not None else None
        if rejected is not None:
            out.notes.append(f"slot {slot}: skipped, {rejected}")
            continue
        out.assets.append(
            DecorAsset(
                slot=slot,
                path=chosen,
                label=label,
                asset_uid=uid,
                batch_id=str(entry.get("batch_id") or ""),
                provider=str(entry.get("provider") or ""),
                model=str(entry.get("model") or ""),
            )
        )
    return out
