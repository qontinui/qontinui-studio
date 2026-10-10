"""Helpers for the library import / approval / export tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from PIL import Image

from qontinui_studio.inbox import ImportOutcome, import_inbox
from qontinui_studio.store import connect
from qontinui_studio.workspace import Workspace

from .conftest import FakeClock

DROP = object()


def write_image(
    path: Path, color: tuple[int, int, int] = (200, 120, 40), size: tuple[int, int] = (12, 8)
) -> Path:
    """An image whose format follows ``path``'s suffix unless overridden by the caller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".gif": "GIF"}
    Image.new("RGB", size, color).save(path, fmt[path.suffix.lower()])
    return path


def entry(**overrides: Any) -> dict[str, Any]:
    """A valid manifest entry; ``key=DROP`` removes a key."""
    base: dict[str, Any] = {
        "file": "hero.png",
        "asset_id": "cafe-hero",
        "provider": "midjourney",
        "model": "v7",
        "account": "studio-mj.1",
        "prompt": "An empty cafe interior in warm light",
        "reference_images": [],
        "generated_on": "2026-10-01",
        "licence_terms_url": "https://example.test/terms",
        "tos_reviewed": "2026-10-02",
        "commercial_use": "yes",
        "watermark": "none",
        "ai_generated": True,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not DROP}


def make_inbox(
    parent: Path,
    entries: list[Any] | None = None,
    name: str = "inbox-1",
    images: dict[str, tuple[int, int, int]] | None = None,
) -> Path:
    """An inbox dir with ``manifest.yaml`` and the named images (default: hero.png)."""
    inbox = parent / name
    inbox.mkdir(parents=True, exist_ok=True)
    for rel, color in (images if images is not None else {"hero.png": (200, 120, 40)}).items():
        write_image(inbox / rel, color)
    (inbox / "manifest.yaml").write_text(
        yaml.safe_dump(entries if entries is not None else [entry()], sort_keys=False)
    )
    return inbox


def do_import(studio_root: Path, inbox: Path, clock: FakeClock | None = None) -> ImportOutcome:
    ws = Workspace(studio_root)
    return import_inbox(connect(ws.db_path), ws, inbox, clock or FakeClock())
