"""``studio library import <inbox-dir>`` — bring images made OUTSIDE the studio
(e.g. by hand in a provider's web UI) into the library with the same D5
provenance a generated asset carries.

The contract is ``<inbox-dir>/manifest.yaml``, described by
``schemas/import-manifest.schema.json``. This module validates it by hand;
``tests/test_library_import.py`` cross-checks the constants below against that
schema file and runs both over the same inputs, so the two cannot drift.

Rules:

* The WHOLE manifest is validated before anything is written. One bad entry
  refuses the import, and every problem is reported with its entry index.
* The manifest is plain data: at most :data:`MAX_MANIFEST_BYTES`, at most
  :data:`MAX_ENTRIES` entries, no YAML anchors, aliases, merge keys or
  duplicate keys. A manifest value never appears in a message unless it is a
  (truncated) string.
* Each file is opened ONCE, from a descriptor on the inbox, walking every path
  component with ``O_NOFOLLOW`` (POSIX; Windows gets a weaker, documented
  fallback — see :func:`_open_inside`), and must be a regular file with one
  link. Its bytes are streamed into a private staging file in the raw store
  (``.studio/raw/imported/.staging/<run>/``)
  while being hashed; the size, SHA-256, format and dimensions all come from
  that one read, and the inbox is never touched again.
* Width, height, SHA-256 and MIME type are MEASURED by decoding the staged
  bytes with Pillow (PNG, JPEG or WebP); the extension is never trusted. The
  pixel count is checked from the header before any pixel is decoded.
* Staged files are renamed into the content-addressed raw store
  (``.studio/raw/imported/<sha[:2]>/<sha>.<ext>``) inside the database write
  transaction; on refusal or rollback every staged file, and every raw file
  this import created, is deleted.
* Re-importing the same bytes under the same asset id is a no-op.
* Imported rows go in batch ``import:<inbox basename>``, whose status is
  ``imported`` — a status the database refuses to move, so an import is never
  published.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
import warnings as py_warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from PIL import Image

from qontinui_studio.errors import ImportRefused
from qontinui_studio.library import APPROVAL_STATUSES, END_OF_STRING, MIMES, is_alias
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.workspace import Workspace

MANIFEST_NAME = "manifest.yaml"
# Every pattern is written so Python ``re`` (with re.ASCII), the jsonschema
# package and ECMA-262 read it the same way: explicit [0-9] rather than \d, an
# explicit ASCII whitespace class rather than \s/\S, and END_OF_STRING rather
# than $ (see library.py). The schema file carries the same strings.
NON_EMPTY_PATTERN = r"[^\t\n\v\f\r ]"
ASSET_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*" + END_OF_STRING
INBOX_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]*" + END_OF_STRING
HTTPS_URL_PATTERN = r"^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?([/?#][!-~]*)?" + END_OF_STRING
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}" + END_OF_STRING
# Seconds are 00-59: a leap second (:60) is refused by both the schema and the
# code (Python's datetime cannot represent it).
RFC3339_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]+)?"
    r"([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])" + END_OF_STRING
)
COMMERCIAL_USE = ("yes", "no", "unknown")
WATERMARKS = ("none", "invisible", "visible", "unknown")
ALIAS_RULE = "letters, digits, '.', '_' or '-' only — never an e-mail address, no whitespace"

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

# Import caps. A file over MAX_FILE_BYTES is refused before it is read; an
# image whose header declares more than MAX_PIXELS pixels is refused before
# its pixel data is decoded (a decompression bomb never expands). The manifest
# itself, its entry count and the bytes one import may read are bounded too.
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_PIXELS = 100_000_000
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ENTRIES = 1000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
_IMPORTED_SUBDIR = "imported"
# Staging lives INSIDE the raw store's directory, so moving a staged file into
# the store is a rename on one filesystem even when .studio/raw is a mount or a
# link to another disk.
_STAGING_SUBDIR = ".staging"
STALE_STAGING_S = 24 * 3600
_CHUNK = 1024 * 1024
_SHOWN_CHARS = 80


def _match(pattern: str, value: object) -> bool:
    return isinstance(value, str) and re.match(pattern, value, re.ASCII) is not None


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and re.search(NON_EMPTY_PATTERN, value, re.ASCII) is not None


def _show(value: object) -> str:
    """A manifest value for a message: a string, quoted and truncated, or
    otherwise only its type name — never the value itself, so an expanded
    structure can never be rendered into an error."""
    if isinstance(value, str):
        cut = value[:_SHOWN_CHARS]
        return repr(cut) + ("…" if len(value) > _SHOWN_CHARS else "")
    return f"a value of type {type(value).__name__}"


# ---- the manifest loader ----------------------------------------------------


class _ManifestLoader(yaml.SafeLoader):
    """Plain data only. YAML 1.2-style scalars: only ``true``/``false`` are
    booleans (so ``commercial_use: yes`` is the string 'yes', not True) and
    dates stay strings (validated here, not coerced by the parser). Anchors,
    aliases, merge keys and duplicate keys are refused."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise yaml.composer.ComposerError(
                None, None, "YAML anchors and aliases are not allowed", event.start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: set[object] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise yaml.constructor.ConstructorError(
                    None, None, "YAML merge keys are not allowed", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
            except TypeError:
                raise yaml.constructor.ConstructorError(
                    None, None, "a mapping key must be a plain scalar", key_node.start_mark
                ) from None
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {_show(key)}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


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


# ---- opening files inside the inbox -------------------------------------------


@dataclass(frozen=True)
class _Opened:
    fd: int
    size: int


def _split(raw: str) -> list[str] | str:
    """Path components of a manifest ``file``, or why it is refused. A path is
    written plainly: no absolute form, no ``..``, and no empty (``//``),
    ``.`` or trailing-separator segments."""
    if "\x00" in raw:
        return "contains a NUL byte"
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        return "is absolute; files must be relative to the inbox"
    parts = re.split(r"[\\/]", raw)
    if ".." in parts:
        return "escapes the inbox ('..')"
    if raw.endswith(("/", "\\")):
        return "ends with a separator; name a file"
    if "" in parts or "." in parts:
        return "has an empty or '.' segment; write the path plainly (a/b.png)"
    return parts


def _check_opened(fd: int, max_bytes: int, cap: str) -> _Opened | str:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if st.st_nlink > 1:
        return "has more than one hard link; refused (it may live outside the inbox)"
    if st.st_size > max_bytes:
        return f"is {st.st_size} bytes, over the {max_bytes}-byte ({cap}) cap"
    return _Opened(fd, st.st_size)


def _open_error(e: OSError, component: str, final: bool) -> str:
    if e.errno == errno.ENOENT:
        return "does not exist in the inbox"
    if e.errno in (errno.ELOOP, errno.EMLINK) or (e.errno == errno.ENOTDIR and not final):
        where = "is a symlink" if final else f"goes through a symlink ({component})"
        return f"{where}, or a non-directory; symlinks are refused"
    return f"cannot be read ({type(e).__name__})"


_DIR_FD = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")


def _open_inside(
    inbox: Path,
    inbox_fd: int | None,
    parts: list[str],
    max_bytes: int | None = None,
    cap: str = "50 MiB",
) -> _Opened | str:
    """Open ``parts`` under the inbox for reading, refusing any symlink.

    On POSIX every component is opened relative to the previous directory's
    descriptor with ``O_NOFOLLOW`` (directories with ``O_DIRECTORY``), and the
    file with ``O_NONBLOCK`` so a FIFO cannot hang the import: a path component
    swapped for a symlink at any moment is refused, because nothing is ever
    re-resolved by name. Every check is then made on the opened descriptor.
    Without ``dir_fd`` support (Windows) it falls back to a name-based walk
    that refuses every component that is a symlink, a junction or any other
    reparse point, opens the file by name, then requires the opened file to be
    the one walked to (same identity) and its resolved path to lie inside the
    inbox. That fallback refuses every link that exists when it looks, but it
    is NOT race-free: a component swapped between the walk and the open can
    still be followed, and only the post-open resolve check stands in the way."""
    if _DIR_FD and inbox_fd is not None:
        held: list[int] = []
        try:
            current = inbox_fd
            for comp in parts[:-1]:
                try:
                    current = os.open(
                        comp, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=current
                    )
                except OSError as e:
                    return _open_error(e, comp, final=False)
                held.append(current)
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            except OSError as e:
                return _open_error(e, parts[-1], final=True)
        finally:
            for h in held:
                os.close(h)
    else:  # the Windows fallback (tests force it on POSIX too)
        path = inbox
        for n, comp in enumerate(parts):
            path = path / comp
            try:
                linked = _is_link_like(path)
            except OSError as e:
                return _open_error(e, comp, final=n == len(parts) - 1)
            if linked:
                return f"goes through a symlink or junction ({comp}); links are refused"
        try:
            before = path.lstat()
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError as e:
            return _open_error(e, parts[-1], final=True)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            os.close(fd)
            return "changed while being imported; refused"
        try:
            inside = path.resolve(strict=True).is_relative_to(inbox.resolve(strict=True))
        except OSError:
            inside = False
        if not inside:
            os.close(fd)
            return "resolves outside the inbox; refused"
    checked = _check_opened(fd, MAX_FILE_BYTES if max_bytes is None else max_bytes, cap)
    if isinstance(checked, str):
        os.close(fd)
    return checked


def _is_link_like(path: Path) -> bool:
    """A symlink, a Windows junction, or any other reparse point. The junction
    and reparse-point checks are no-ops where the platform has no such thing."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or path.is_junction():
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(st, "st_file_attributes", 0) & reparse)


@contextlib.contextmanager
def _inbox_dir_fd(inbox: Path) -> Iterator[int | None]:
    if not _DIR_FD:
        yield None
        return
    try:
        fd = os.open(inbox, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise ImportRefused(
            "the inbox cannot be opened", [f"inbox: cannot be opened ({_errno_name(e)})"]
        ) from e
    try:
        yield fd
    finally:
        os.close(fd)


# ---- staging: one streamed read per file ---------------------------------------


def _errno_name(e: OSError) -> str:
    """An OSError named without its message (which carries absolute paths)."""
    return errno.errorcode.get(e.errno or 0, type(e).__name__)


class _Staging:
    """Private copies of the inbox files under
    ``.studio/raw/imported/.staging/<run>/``, named by SHA-256. Bytes are hashed
    while they are copied, so each inbox file is read exactly once and never
    held in memory whole."""

    def __init__(self, ws: Workspace) -> None:
        self.parent = ws.raw_dir / _IMPORTED_SUBDIR / _STAGING_SUBDIR
        self.dir = self.parent / uuid.uuid4().hex
        self.total = 0

    def start(self) -> None:
        """Create this run's directory. The shared parent is never removed by
        an import, but tolerate it vanishing between the two mkdirs anyway."""
        for _ in range(3):
            try:
                self.dir.mkdir(parents=True)
                return
            except FileNotFoundError:
                continue
            except OSError as e:
                raise _staging_refused(e) from e
        raise ImportRefused(
            "could not create the import staging directory",
            ["staging: could not create this import's staging directory (it kept vanishing)"],
        )

    def stage(self, opened: _Opened) -> tuple[str, int, Path] | str:
        if self.total + opened.size > MAX_TOTAL_BYTES:
            return f"would take this import past its {MAX_TOTAL_BYTES}-byte (2 GiB) total cap"
        tmp = self.dir / f".partial-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        size = 0
        try:
            dst = tmp.open("wb")
        except OSError as e:
            return f"could not be staged ({_errno_name(e)})"
        with dst, os.fdopen(opened.fd, "rb", closefd=False) as src:
            while True:
                try:
                    chunk = src.read(_CHUNK)
                except OSError as e:
                    return f"cannot be read ({_errno_name(e)})"
                if not chunk:
                    break
                size += len(chunk)
                self.total += len(chunk)
                if size > MAX_FILE_BYTES:
                    return f"is over the {MAX_FILE_BYTES}-byte (50 MiB) cap"
                if self.total > MAX_TOTAL_BYTES:
                    return (
                        f"would take this import past its {MAX_TOTAL_BYTES}-byte (2 GiB) total cap"
                    )
                digest.update(chunk)
                try:
                    dst.write(chunk)
                except OSError as e:
                    return f"could not be staged ({_errno_name(e)})"
            try:
                dst.flush()
                os.fsync(dst.fileno())
            except OSError as e:
                return f"could not be staged ({_errno_name(e)})"
        # (A refused partial copy is removed with the whole run by discard().)
        sha = digest.hexdigest()
        staged = self.dir / sha
        try:
            if staged.exists():
                tmp.unlink()
            else:
                os.replace(tmp, staged)
        except OSError as e:
            return f"could not be staged ({_errno_name(e)})"
        return sha, size, staged

    def discard(self) -> None:
        """Remove this run's directory only; the shared parent stays, so a
        concurrent import's mkdir under it can never lose its parent."""
        shutil.rmtree(self.dir, ignore_errors=True)

    def sweep_stale(self, now: float) -> None:
        """Delete run directories older than STALE_STAGING_S: left by an import
        that was killed. Called under the database write lock, and no live
        import takes a day to validate."""
        try:
            runs = list(self.parent.iterdir())
        except OSError:
            return
        for run in runs:
            if run == self.dir:
                continue
            with contextlib.suppress(OSError):
                if now - run.lstat().st_mtime > STALE_STAGING_S:
                    shutil.rmtree(run, ignore_errors=True)


def _staging_refused(e: OSError) -> ImportRefused:
    return ImportRefused(
        "the import could not use its staging directory",
        [f"staging: could not use this import's staging directory ({_errno_name(e)})"],
    )


def _decode(path: Path) -> tuple[str, int, int] | str:
    """(Pillow format, width, height) of a fully decodable PNG/JPEG/WebP, or
    the reason it is not one."""
    too_big = f"is over the {MAX_PIXELS}-pixel (100 megapixel) cap"
    try:
        with py_warnings.catch_warnings():
            # The cap below is the decision; Pillow's own bomb warning is noise.
            py_warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as img:
                fmt = img.format or ""
                if fmt not in MIMES:
                    return f"is {fmt or 'an unknown format'}, not PNG/JPEG/WebP"
                # Header dimensions only: checked BEFORE any pixel data is decoded,
                # so a decompression bomb is refused without being expanded.
                header_w, header_h = img.size
                if header_w * header_h > MAX_PIXELS:
                    return f"{too_big} ({header_w}x{header_h} in its header)"
                img.verify()
            with Image.open(path) as img:
                img.load()
                width, height = img.size
    except Image.DecompressionBombError:
        # Pillow refuses at open (before we can read the header) beyond 2x its
        # own limit; that is far over our cap too.
        return too_big
    except Exception as e:
        return f"is not a decodable PNG/JPEG/WebP image ({type(e).__name__})"
    return fmt, width, height


# ---- validation ------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """A validated entry. It holds the staged copy's path and what was
    measured from it — never the file's bytes."""

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
    staged: Path
    sha256: str
    size: int
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
    if not _match(HTTPS_URL_PATTERN, value):
        return False
    parts = urlsplit(str(value))
    return parts.scheme == "https" and bool(parts.hostname)


def _iso_date(value: object) -> str | None:
    """``YYYY-MM-DD`` that is a real date, else None."""
    if not _match(DATE_PATTERN, value):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _date_or_rfc3339(value: object) -> str | None:
    """An ISO date or an RFC 3339 timestamp (with its offset), as given."""
    as_date = _iso_date(value)
    if as_date is not None:
        return as_date
    if not _match(RFC3339_PATTERN, value):
        return None
    text = str(value)
    try:
        datetime.fromisoformat(text.replace("z", "Z").replace("t", "T"))
    except ValueError:
        return None
    return text


def _validate_entry(
    inbox: Path,
    inbox_fd: int | None,
    staging: _Staging,
    index: int,
    raw: object,
    errors: list[str],
    warnings: list[str],
) -> _Entry | None:
    """Every structural and file-level rule for one entry. Problems are
    appended to ``errors`` (all of them, not just the first)."""

    def err(name: str, message: str) -> None:
        errors.append(f"entry {index}: {name}: {message}")

    if not isinstance(raw, dict):
        errors.append(f"entry {index}: must be a mapping, not {_show(raw)}")
        return None
    start = len(errors)
    known = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    for key in sorted((k for k in raw if k not in known), key=_show):
        err(_show(key) if not isinstance(key, str) else key[:_SHOWN_CHARS], "unknown field")
    for name in REQUIRED_FIELDS:
        if name not in raw:
            err(name, "required")

    def text(name: str) -> str:
        value = raw.get(name)
        if name in raw and not _non_empty(value):
            err(name, f"must be a non-empty string, not {_show(value)}")
            return ""
        return value if isinstance(value, str) else ""

    asset_id = text("asset_id")
    if asset_id and not _match(ASSET_ID_PATTERN, asset_id):
        err("asset_id", f"{_show(asset_id)} must be lowercase letters, digits and '-'")
    provider = text("provider")
    model = text("model")
    prompt = text("prompt")
    account = text("account")
    if account and not is_alias(account):
        err("account", f"{_show(account)} must be an alias ({ALIAS_RULE})")

    references: list[Any] = []
    refs = raw.get("reference_images", [])
    if not isinstance(refs, list):
        err("reference_images", f"must be a list, not {_show(refs)}")
    else:
        for n, ref in enumerate(refs):
            if isinstance(ref, str):
                if not _match(ASSET_ID_PATTERN, ref):
                    err(
                        f"reference_images[{n}]",
                        f"{_show(ref)} is not an asset id; local paths are refused — name an "
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
                elif not _non_empty(ref["licence"]):
                    err(f"reference_images[{n}].licence", "must be a non-empty string")
                else:
                    references.append({"url": ref["url"], "licence": ref["licence"]})
            else:
                err(
                    f"reference_images[{n}]",
                    f"must be an asset_id or {{url, licence}}, not {_show(ref)}",
                )

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
        err("approved_by", f"{_show(approved_by)} must be an alias ({ALIAS_RULE})")
    approved_on: str | None = None
    if "approved_on" in raw:
        approved_on = _iso_date(raw["approved_on"])
        if approved_on is None:
            err("approved_on", "must be an ISO date (YYYY-MM-DD)")

    measured: tuple[str, int, Path, str, int, int] | None = None
    if "file" in raw:
        name = raw["file"]
        if not _non_empty(name):
            err("file", "must be a non-empty relative path")
        else:
            parts = _split(name)
            opened = parts if isinstance(parts, str) else _open_inside(inbox, inbox_fd, parts)
            if isinstance(opened, str):
                err("file", f"{_show(name)} {opened}")
            else:
                try:
                    staged = staging.stage(opened)
                finally:
                    os.close(opened.fd)
                if isinstance(staged, str):
                    err("file", f"{_show(name)} {staged}")
                else:
                    sha, size, path = staged
                    decoded = _decode(path)
                    if isinstance(decoded, str):
                        err("file", f"{_show(name)} {decoded}")
                    else:
                        measured = (sha, size, path, *decoded)

    if len(errors) > start or measured is None:
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

    sha, size, path, fmt, width, height = measured
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
        staged=path,
        sha256=sha,
        size=size,
        width=width,
        height=height,
        image_format=fmt,
    )


def _refuse(message: str) -> ImportRefused:
    return ImportRefused(message, [f"{MANIFEST_NAME}: {message}"])


def _load_manifest(inbox: Path, inbox_fd: int | None) -> list[Any]:
    """Read the manifest through the same symlink-refusing open as the images,
    bounded to MAX_MANIFEST_BYTES, and parse it as plain data."""
    opened = _open_inside(inbox, inbox_fd, [MANIFEST_NAME], MAX_MANIFEST_BYTES, "1 MiB")
    if isinstance(opened, str):
        if " cap" in opened:
            raise _refuse(opened)
        raise _refuse(f"missing or unusable ({opened})")
    try:
        with os.fdopen(opened.fd, "rb", closefd=False) as f:
            data = f.read(MAX_MANIFEST_BYTES + 1)
    except OSError as e:
        raise _refuse(f"unreadable ({type(e).__name__})") from e
    finally:
        os.close(opened.fd)
    if len(data) > MAX_MANIFEST_BYTES:
        raise _refuse(f"is over the {MAX_MANIFEST_BYTES}-byte (1 MiB) cap")
    try:
        doc = yaml.load(data.decode("utf-8"), Loader=_ManifestLoader)
    except UnicodeDecodeError as e:
        raise _refuse("unreadable (not UTF-8)") from e
    except yaml.YAMLError as e:
        raise _refuse(f"not valid YAML: {str(e)[:500]}") from e
    except RecursionError as e:
        raise _refuse("nested too deeply") from e
    if not isinstance(doc, list) or not doc:
        raise _refuse("must be a non-empty list of entries")
    if len(doc) > MAX_ENTRIES:
        raise _refuse(f"has {len(doc)} entries, over the {MAX_ENTRIES}-entry cap")
    return doc


def _raw_store_path(ws: Workspace, entry: _Entry) -> Path:
    ext = EXTENSIONS[entry.image_format]
    return ws.raw_dir / _IMPORTED_SUBDIR / entry.sha256[:2] / f"{entry.sha256}.{ext}"


def _install(staged: Path, raw_path: Path) -> bool:
    """Move a staged file into the raw store. Returns True when this call
    created the raw file (so a rollback must delete it)."""
    if raw_path.is_file() and sha256_file(raw_path) == raw_path.stem:
        return False
    existed = raw_path.exists()
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, raw_path)  # same filesystem: staging is inside the raw store's dir
    return not existed


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
    if not _match(INBOX_NAME_PATTERN, inbox.name):
        raise ImportRefused(
            f"inbox name {_show(inbox.name)} is not a valid batch name",
            [
                f"inbox name {_show(inbox.name)} must match ^[a-z0-9][a-z0-9._-]*$ "
                "(lowercase letters, digits, '.', '_', '-')"
            ],
        )
    batch_id = f"import:{inbox.name}"
    staging = _Staging(ws)
    try:
        staging.start()
        with _inbox_dir_fd(inbox) as inbox_fd:
            doc = _load_manifest(inbox, inbox_fd)
            errors: list[str] = []
            outcome = ImportOutcome(batch_id=batch_id)
            entries: list[_Entry] = []
            for index, raw in enumerate(doc):
                entry = _validate_entry(
                    inbox, inbox_fd, staging, index, raw, errors, outcome.warnings
                )
                if entry is not None:
                    entries.append(entry)
        _check_duplicates(entries, errors)
        manifest_ids = {
            r["asset_id"] for r in doc if isinstance(r, dict) and isinstance(r.get("asset_id"), str)
        }
        _commit(conn, ws, staging, batch_id, inbox, entries, manifest_ids, errors, outcome, clock)
    finally:
        staging.discard()
    return outcome


def _check_duplicates(entries: list[_Entry], errors: list[str]) -> None:
    seen: dict[tuple[str, str], int] = {}
    for entry in entries:
        key = (entry.asset_id, entry.sha256)
        if key in seen:
            errors.append(
                f"entry {entry.index}: file: same bytes and asset_id as entry {seen[key]}"
            )
        seen.setdefault(key, entry.index)


def _commit(
    conn: sqlite3.Connection,
    ws: Workspace,
    staging: _Staging,
    batch_id: str,
    inbox: Path,
    entries: list[_Entry],
    manifest_ids: set[str],
    errors: list[str],
    outcome: ImportOutcome,
    clock: Callable[[], datetime],
) -> None:
    created: list[Path] = []
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Under the write lock no other import is committing; a staging run
        # older than a day belongs to an import that was killed.
        staging.sweep_stale(time.time())
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
                "SELECT * FROM assets WHERE origin = 'imported' AND asset_id = ? AND sha256 = ?",
                (entry.asset_id, entry.sha256),
            ).fetchone()
            if existing is not None:
                outcome.skipped.append(existing["uid"])
                drift = _drift(existing, entry)
                if drift:
                    outcome.warnings.append(
                        f"entry {entry.index}: already imported as {existing['uid']}; the "
                        f"manifest now differs in {drift} and is NOT applied (an import "
                        "never rewrites provenance; use library approve/reject for approval)"
                    )
                continue
            if not batch_exists:
                conn.execute(
                    "INSERT INTO batches (id, origin, targets_json, created_at, status, no_publish) "
                    "VALUES (?, 'imported', '[]', ?, 'imported', 1)",
                    (batch_id, now),
                )
                batch_exists = True
            raw_path = _raw_store_path(ws, entry)
            if _install(entry.staged, raw_path):
                created.append(raw_path)
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
    except BaseException as e:
        conn.rollback()
        # The rows are gone; so are the raw files this import put there.
        for path in created:
            with contextlib.suppress(OSError):
                path.unlink()
        if isinstance(e, OSError):
            # Never relay the OSError text: it carries absolute local paths.
            raise ImportRefused(
                "the import failed while writing to the library; nothing imported",
                [
                    "write: could not install the files into the raw store "
                    f"({_errno_name(e)}); nothing imported"
                ],
            ) from e
        raise


def _drift(row: sqlite3.Row, entry: _Entry) -> list[str]:
    """Recorded fields an identical-bytes re-import would have set differently."""
    wanted = {
        "provider": entry.provider,
        "model": entry.model,
        "account": entry.account,
        "prompt": entry.prompt,
        "reference_images_json": json.dumps(entry.reference_images, sort_keys=True),
        "generated_on": entry.generated_on,
        "licence_terms_url": entry.licence_terms_url,
        "licence_tos_reviewed": entry.tos_reviewed,
        "licence_commercial_use": entry.commercial_use,
        "watermark": entry.watermark,
        "approval_status": entry.status,
        "approval_by": entry.approved_by,
        "approval_on": entry.approved_on,
    }
    return sorted(k for k, v in wanted.items() if row[k] != v)


def _entry_order(line: str) -> tuple[int, str]:
    m = re.match(r"^entry (\d+): ", line)
    return (int(m.group(1)) if m else -1, line)
