"""``studio library import <inbox-dir>`` — bring images made OUTSIDE the studio
(e.g. by hand in a provider's web UI) into the library with the same D5
provenance a generated asset carries.

The contract is ``<inbox-dir>/manifest.yaml``, described by
``schemas/import-manifest.schema.json``. This module validates it by hand
(``jsonschema`` is not a dependency); ``tests/test_library_import.py``
cross-checks the constants below against that schema file, so the two cannot
drift.

Rules:

* The WHOLE manifest is validated before anything is written. One bad entry
  refuses the import, and every problem is reported with its entry index.
* Width, height, SHA-256 and MIME type are MEASURED by decoding the bytes with
  Pillow (PNG, JPEG or WebP); the file extension is never trusted.
* Each file is copied into the content-addressed raw store
  (``.studio/raw/imported/<sha[:2]>/<sha>.<ext>``); the inbox is never
  referenced again.
* Re-importing the same bytes under the same asset id is a no-op.
* Imported rows go in batch ``import:<inbox basename>``, whose status is
  ``imported`` — a status the database refuses to move, so an import is never
  published.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from PIL import Image

from qontinui_studio.errors import ImportRefused
from qontinui_studio.library import ALIAS_PATTERN, APPROVAL_STATUSES, MIMES, is_alias
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.publish import atomic_write
from qontinui_studio.workspace import Workspace

MANIFEST_NAME = "manifest.yaml"
ASSET_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
INBOX_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
COMMERCIAL_USE = ("yes", "no", "unknown")
WATERMARKS = ("none", "invisible", "visible", "unknown")
HTTPS_URL_PATTERN = r"^https://[^\s/?#]+[^\s]*$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
RFC3339_PATTERN = r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"

REQUIRED_FIELDS = (
    "file",
    "asset_id",
    "provider",
    "model",
    "account",
    "prompt",
    "generated_on",
    "licence_terms_url",
    "commercial_use",
    "watermark",
    "ai_generated",
)
OPTIONAL_FIELDS = ("reference_images", "tos_reviewed", "status", "approved_by", "approved_on")
EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
_IMPORTED_SUBDIR = "imported"


class _ManifestLoader(yaml.SafeLoader):
    """YAML 1.2-style scalars: only ``true``/``false`` are booleans (so
    ``commercial_use: yes`` is the string 'yes', not True) and dates stay
    strings (validated here, not coerced by the parser)."""


_ManifestLoader.yaml_implicit_resolvers = {
    first: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_ManifestLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


@dataclass(frozen=True)
class _Entry:
    index: int
    asset_id: str
    provider: str
    model: str
    account: str
    prompt: str
    reference_images: list[Any]
    generated_on: str
    licence_terms_url: str
    tos_reviewed: str | None
    commercial_use: str
    watermark: str
    status: str
    approved_by: str | None
    approved_on: str | None
    data: bytes
    sha256: str
    width: int
    height: int
    image_format: str


@dataclass
class ImportOutcome:
    batch_id: str
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _is_https_url(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(HTTPS_URL_PATTERN, value) is None:
        return False
    parts = urlsplit(value)
    return parts.scheme == "https" and bool(parts.hostname)


def _iso_date(value: object) -> str | None:
    """``YYYY-MM-DD`` that is a real date, else None."""
    if not isinstance(value, str) or re.fullmatch(DATE_PATTERN, value) is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _date_or_rfc3339(value: object) -> str | None:
    """An ISO date or an RFC 3339 timestamp (with its offset), as given."""
    as_date = _iso_date(value)
    if as_date is not None:
        return as_date
    if not isinstance(value, str) or re.fullmatch(RFC3339_PATTERN, value) is None:
        return None
    try:
        datetime.fromisoformat(value.replace("z", "Z").replace("t", "T"))
    except ValueError:
        return None
    return value


def _inbox_file(inbox: Path, raw: object) -> tuple[Path | None, str | None]:
    """Resolve an entry's ``file`` inside the inbox, or say why not. Refuses an
    absolute path, any ``..`` component, a symlink anywhere along the path, and
    anything that is not a regular file inside the inbox."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "must be a non-empty relative path"
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        return None, f"'{raw}' is absolute; files must be relative to the inbox"
    parts = [p for p in re.split(r"[\\/]", raw) if p not in ("", ".")]
    if not parts:
        return None, "must name a file"
    if ".." in parts:
        return None, f"'{raw}' escapes the inbox ('..')"
    current = inbox
    for part in parts:
        current = current / part
        if current.is_symlink():
            return None, f"'{raw}' goes through a symlink ({part}); symlinks are refused"
    if not current.exists():
        return None, f"'{raw}' does not exist in the inbox"
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(inbox):
        return None, f"'{raw}' resolves outside the inbox"
    if not resolved.is_file():
        return None, f"'{raw}' is not a regular file"
    return resolved, None


def _decode(data: bytes) -> tuple[str, int, int] | str:
    """(Pillow format, width, height) of a fully decodable PNG/JPEG/WebP, or
    the reason it is not one."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            if fmt not in MIMES:
                return f"is {fmt or 'an unknown format'}, not PNG/JPEG/WebP"
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            width, height = img.size
    except Exception as e:
        return f"is not a decodable PNG/JPEG/WebP image ({type(e).__name__})"
    return fmt, width, height


def _validate_entry(
    inbox: Path, index: int, raw: object, errors: list[str], warnings: list[str]
) -> _Entry | None:
    """Every structural and file-level rule for one entry. Problems are
    appended to ``errors`` (all of them, not just the first)."""

    def err(name: str, message: str) -> None:
        errors.append(f"entry {index}: {name}: {message}")

    if not isinstance(raw, dict):
        errors.append(f"entry {index}: must be a mapping")
        return None
    start = len(errors)
    known = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    for key in sorted(str(k) for k in raw if str(k) not in known):
        err(key, "unknown field")
    for name in REQUIRED_FIELDS:
        if name not in raw:
            err(name, "required")

    def text(name: str) -> str:
        value = raw.get(name)
        if name in raw and (not isinstance(value, str) or not value.strip()):
            err(name, "must be a non-empty string")
            return ""
        return value if isinstance(value, str) else ""

    asset_id = text("asset_id")
    if asset_id and re.fullmatch(ASSET_ID_PATTERN, asset_id) is None:
        err("asset_id", f"'{asset_id}' must match {ASSET_ID_PATTERN}")
    provider = text("provider")
    model = text("model")
    prompt = text("prompt")
    account = text("account")
    if account and not is_alias(account):
        err(
            "account",
            f"'{account}' must be an alias matching {ALIAS_PATTERN} "
            "(never an e-mail address, no whitespace)",
        )

    references: list[Any] = []
    refs = raw.get("reference_images", [])
    if not isinstance(refs, list):
        err("reference_images", "must be a list")
    else:
        for n, ref in enumerate(refs):
            if isinstance(ref, str):
                if re.fullmatch(ASSET_ID_PATTERN, ref) is None:
                    err(
                        f"reference_images[{n}]",
                        f"'{ref}' is not an asset id; local paths are refused — name an "
                        "asset_id in this library or give {url, licence}",
                    )
                elif ref == asset_id:
                    err(f"reference_images[{n}]", "an asset cannot reference itself")
                references.append(ref)
            elif isinstance(ref, dict):
                if set(ref) != {"url", "licence"}:
                    err(f"reference_images[{n}]", "a mapping must have exactly url and licence")
                elif not _is_https_url(ref["url"]):
                    err(f"reference_images[{n}].url", "must be an https URL")
                elif not isinstance(ref["licence"], str) or not ref["licence"].strip():
                    err(f"reference_images[{n}].licence", "must be a non-empty string")
                else:
                    references.append({"url": ref["url"], "licence": ref["licence"]})
            else:
                err(f"reference_images[{n}]", "must be an asset_id or {url, licence}")

    generated_on = _date_or_rfc3339(raw.get("generated_on"))
    if "generated_on" in raw and generated_on is None:
        err("generated_on", "must be an ISO date (YYYY-MM-DD) or an RFC 3339 timestamp")

    terms_url = raw.get("licence_terms_url")
    if "licence_terms_url" in raw and not _is_https_url(terms_url):
        err("licence_terms_url", "must be an https URL")

    tos_reviewed: str | None = None
    if "tos_reviewed" in raw:
        tos_reviewed = _iso_date(raw["tos_reviewed"])
        if tos_reviewed is None:
            err("tos_reviewed", "must be an ISO date (YYYY-MM-DD)")

    commercial_use = raw.get("commercial_use")
    if "commercial_use" in raw and commercial_use not in COMMERCIAL_USE:
        err("commercial_use", f"must be one of {'|'.join(COMMERCIAL_USE)}")
    watermark = raw.get("watermark")
    if "watermark" in raw and watermark not in WATERMARKS:
        err("watermark", f"must be one of {'|'.join(WATERMARKS)}")
    if "ai_generated" in raw and raw["ai_generated"] is not True:
        err("ai_generated", "must be true (the library holds AI-generated assets only)")

    status = raw.get("status", "candidate")
    if status not in APPROVAL_STATUSES:
        err("status", f"must be one of {'|'.join(APPROVAL_STATUSES)}")
    approved_by = raw.get("approved_by")
    if "approved_by" in raw and not is_alias(approved_by):
        err(
            "approved_by",
            f"'{approved_by}' must be an alias matching {ALIAS_PATTERN} "
            "(never an e-mail address, no whitespace)",
        )
    approved_on: str | None = None
    if "approved_on" in raw:
        approved_on = _iso_date(raw["approved_on"])
        if approved_on is None:
            err("approved_on", "must be an ISO date (YYYY-MM-DD)")

    path, why = _inbox_file(inbox, raw.get("file")) if "file" in raw else (None, None)
    decoded: tuple[str, int, int] | str | None = None
    data = b""
    if why is not None:
        err("file", why)
    elif path is not None:
        data = path.read_bytes()
        decoded = _decode(data)
        if isinstance(decoded, str):
            err("file", f"'{raw['file']}' {decoded}")

    if len(errors) > start or not isinstance(decoded, tuple):
        return None

    # Normalisation (warnings, not refusals).
    if commercial_use == "yes" and tos_reviewed is None:
        warnings.append(
            f"entry {index}: commercial_use 'yes' without tos_reviewed is recorded as 'unknown'"
        )
        commercial_use = "unknown"
    if status in ("approved", "rejected") and (approved_by is None or approved_on is None):
        warnings.append(
            f"entry {index}: status '{status}' without approved_by and approved_on is "
            "recorded as 'candidate'"
        )
        status = "candidate"
    if status == "candidate" and (approved_by is not None or approved_on is not None):
        warnings.append(f"entry {index}: approved_by/approved_on ignored for a candidate")
        approved_by = approved_on = None
    if status == "approved" and commercial_use != "yes":
        err(
            "status",
            f"cannot be approved: commercial_use is '{commercial_use}', not 'yes' (D5)",
        )
        return None

    fmt, width, height = decoded
    return _Entry(
        index=index,
        asset_id=asset_id,
        provider=provider,
        model=model,
        account=account,
        prompt=prompt,
        reference_images=references,
        generated_on=str(generated_on),
        licence_terms_url=str(terms_url),
        tos_reviewed=tos_reviewed,
        commercial_use=str(commercial_use),
        watermark=str(watermark),
        status=str(status),
        approved_by=approved_by,
        approved_on=approved_on,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        width=width,
        height=height,
        image_format=fmt,
    )


def _load_manifest(inbox: Path) -> list[Any]:
    path = inbox / MANIFEST_NAME
    if path.is_symlink() or not path.is_file():
        raise ImportRefused(
            f"no {MANIFEST_NAME} in {inbox}", [f"{MANIFEST_NAME}: missing (or a symlink)"]
        )
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_ManifestLoader)
    except yaml.YAMLError as e:
        raise ImportRefused(f"{MANIFEST_NAME} is not valid YAML", [f"{MANIFEST_NAME}: {e}"]) from e
    if not isinstance(doc, list) or not doc:
        raise ImportRefused(
            f"{MANIFEST_NAME} must be a non-empty list of entries",
            [f"{MANIFEST_NAME}: must be a non-empty list of entries"],
        )
    return doc


def _raw_store_path(ws: Workspace, entry: _Entry) -> Path:
    ext = EXTENSIONS[entry.image_format]
    return ws.raw_dir / _IMPORTED_SUBDIR / entry.sha256[:2] / f"{entry.sha256}.{ext}"


def import_inbox(
    conn: sqlite3.Connection,
    ws: Workspace,
    inbox_dir: Path,
    clock: Callable[[], datetime],
) -> ImportOutcome:
    """Validate ``<inbox_dir>/manifest.yaml`` whole, then import every new entry
    in one transaction. Raises :class:`ImportRefused` (nothing written) when
    any entry is invalid."""
    if not inbox_dir.is_dir():
        raise ImportRefused(f"{inbox_dir} is not a directory", [f"{inbox_dir}: not a directory"])
    inbox = inbox_dir.resolve(strict=True)
    if re.fullmatch(INBOX_NAME_PATTERN, inbox.name) is None:
        raise ImportRefused(
            f"inbox name '{inbox.name}' is not a valid batch name",
            [f"inbox name '{inbox.name}' must match {INBOX_NAME_PATTERN}"],
        )
    batch_id = f"import:{inbox.name}"
    doc = _load_manifest(inbox)

    errors: list[str] = []
    outcome = ImportOutcome(batch_id=batch_id)
    entries: list[_Entry] = []
    for index, raw in enumerate(doc):
        entry = _validate_entry(inbox, index, raw, errors, outcome.warnings)
        if entry is not None:
            entries.append(entry)
    seen: dict[tuple[str, str], int] = {}
    for entry in entries:
        key = (entry.asset_id, entry.sha256)
        if key in seen:
            errors.append(
                f"entry {entry.index}: file: same bytes and asset_id as entry {seen[key]}"
            )
        seen.setdefault(key, entry.index)
    manifest_ids = {
        r["asset_id"] for r in doc if isinstance(r, dict) and isinstance(r.get("asset_id"), str)
    }

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Checks against the library run under the write lock, so they hold
        # for the rows written below.
        library_ids = {r["asset_id"] for r in conn.execute("SELECT DISTINCT asset_id FROM assets")}
        for entry in entries:
            for n, ref in enumerate(entry.reference_images):
                if isinstance(ref, str) and ref not in library_ids | manifest_ids:
                    errors.append(
                        f"entry {entry.index}: reference_images[{n}]: asset_id '{ref}' "
                        "is not in this library"
                    )
        batch = conn.execute("SELECT origin FROM batches WHERE id = ?", (batch_id,)).fetchone()
        batch_exists = batch is not None
        if batch is not None and batch["origin"] != "imported":
            errors.append(f"batch {batch_id} exists and is not an import batch")
        if errors:
            raise ImportRefused(
                f"{inbox / MANIFEST_NAME}: {len(errors)} problem(s); nothing imported",
                sorted(errors, key=_entry_order),
            )

        now = clock().isoformat()
        for entry in entries:
            existing = conn.execute(
                "SELECT uid FROM assets WHERE origin = 'imported' AND asset_id = ? AND sha256 = ?",
                (entry.asset_id, entry.sha256),
            ).fetchone()
            if existing is not None:
                outcome.skipped.append(existing["uid"])
                continue
            if not batch_exists:
                conn.execute(
                    "INSERT INTO batches (id, origin, targets_json, created_at, status, no_publish) "
                    "VALUES (?, 'imported', '[]', ?, 'imported', 1)",
                    (batch_id, now),
                )
                batch_exists = True
            raw_path = _raw_store_path(ws, entry)
            if not raw_path.is_file() or sha256_file(raw_path) != entry.sha256:
                atomic_write(raw_path, entry.data)
            uid = f"import-{entry.asset_id}-{entry.sha256[:16]}"
            conn.execute(
                "INSERT INTO assets (uid, asset_id, origin, batch_id, provider, model, prompt, "
                "created_at, raw_path, sha256, width, height, mime, ai_generated, "
                "licence_commercial_use, licence_source, licence_tos_reviewed, licence_terms_url, "
                "watermark, watermark_visible, account, reference_images_json, generated_on, "
                "approval_status, approval_by, approval_on) "
                "VALUES (?,?,'imported',?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    entry.asset_id,
                    batch_id,
                    entry.provider,
                    entry.model,
                    entry.prompt,
                    now,
                    str(raw_path),
                    entry.sha256,
                    entry.width,
                    entry.height,
                    MIMES[entry.image_format],
                    entry.commercial_use,
                    entry.licence_terms_url,
                    entry.tos_reviewed,
                    entry.licence_terms_url,
                    entry.watermark,
                    {"visible": 1, "invisible": 0, "none": 0}.get(entry.watermark),
                    entry.account,
                    json.dumps(entry.reference_images, sort_keys=True),
                    entry.generated_on,
                    entry.status,
                    entry.approved_by,
                    entry.approved_on,
                ),
            )
            outcome.imported.append(uid)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return outcome


def _entry_order(line: str) -> tuple[int, str]:
    m = re.match(r"^entry (\d+): ", line)
    return (int(m.group(1)) if m else -1, line)
