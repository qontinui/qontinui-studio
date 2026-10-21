"""Publish a launch bundle when every automated gate passes (D7), notify the
operator with a one-step unpublish, and unpublish on request.

Gates over a bundle:

* ``d1_record`` — the bundle carries a build record (decoration never stands
  in for one);
* ``d3_allowlist`` — the bundled ``build-record.json`` re-passes the D3
  allowlist and its digest matches;
* ``integrity`` — every file is byte-identical to what assembly wrote;
* ``d5_provenance`` / ``d5_licence`` — each decorative image is a library
  variant with full provenance and a licence fit for the target;
* ``d6`` — screenshots are captured pixels shown as screenshots, AI images are
  shown only inside a labelled ``data-ai-generated='true'`` figure;
* ``footer`` — every page links "Built by the Qontinui software factory" to the
  public build record;
* ``foreign_file`` — every file in the publish dir (other than the studio's
  lock and journals) is a file the live launch placed and still holds the
  bytes it placed; anything else (a hand edit, someone's own page) refuses;
* ``already_live`` — a launch that is already the live publication is refused;
* D7, preview first and bound to THIS launch, with no waiver (the asset
  pipeline's binding): the bundle is written to ``publish.preview_dir``
  (outside the studio root, overlapping no site's dirs); ``d7_preview`` — the
  preview serves exactly this bundle's ``launch-manifest.json`` (sha256);
  ``d7_navigate`` — the preview's UI Bridge is driven to
  ``preview_url?studio_launch=<id>`` and its snapshot's ``page.url`` must be
  exactly that, before and after page health; ``d7_page_health`` — OK or
  WARNING; ``d7_images`` — every image on the landing page rendered and
  loaded (``naturalWidth > 0``). Nothing live changes until all pass. Only
  files the previous studio-written preview manifest lists are ever deleted.

The live change follows the asset pipeline's discipline: the OS lock on the
publish dir (and the preview dir while it is written) is held from the first
check to the notice; publish AND unpublish are journalled
(:class:`LaunchJournal`, ``.studio-journal-<launch>.json``, the pipeline's
protocol with bundle-relative paths), every original is re-checked by sha256
right before it moves, and a journal is VALIDATED before recovery acts on it.
The DB transaction — gate results included — is the commit point; anything
after it (cleanup, the notice) is a warning, never a refusal. Recovery replays
a journal forward if the launch committed, backward otherwise, and notifies
the operator; it refuses launches orphaned in ``publishing`` and re-sends the
published notice for a live launch whose ``notified_at`` is unset.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from qontinui_studio.commands import studio_command
from qontinui_studio.errors import JournalError, StudioError
from qontinui_studio.gates import (
    BridgeFactory,
    GateResult,
    HttpUiBridge,
    ManifestFetch,
    PreviewSpec,
    d5_provenance,
    http_manifest_fetch,
    licence_verdict,
    page_health_gate,
    preview_manifest_gate,
    preview_page_gates,
    with_query,
)
from qontinui_studio.launch.bundle import (
    BUNDLE_SCHEMA,
    FOOTER_TEXT,
    MANIFEST,
    PAGES,
    BundleRefused,
    hidden_component,
    media_group,
    render_pages,
)
from qontinui_studio.launch.product import LaunchProduct
from qontinui_studio.launch.record import (
    MAX_EXCLUDED_REPOS,
    BuildRecordClient,
    BuildRecordInvalid,
    FetchedRecord,
    FetchOutcome,
    NoBuildRecord,
    allowlist_violations,
    canonical_sha256,
    excluded_names,
    parse_document,
    redacted,
)
from qontinui_studio.library import Library
from qontinui_studio.locks import LOCK_NAME, site_locks
from qontinui_studio.notify import Delivery, Notice, Notifier
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.publish import (
    JOURNAL_PREFIX,
    ForeignFile,
    Moved,
    Waiter,
    atomic_copy,
    atomic_write,
)


class BundleChanged(StudioError):
    """A bundle file's bytes differ from what the gates judged."""

    def __init__(self, rel: str) -> None:
        super().__init__(f"{rel} changed after the gates judged it")
        self.rel = rel


class LaunchNotPublished(StudioError):
    """Unpublish was asked of a launch that is not the live publication."""


@dataclass
class LaunchPublishResult:
    launch_id: str
    published: bool
    gate_results: list[GateResult] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    delivery: Delivery | None = None
    # Post-commit problems (cleanup, notice): the launch IS published.
    warnings: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gate_results if g.failed]


def unpublish_command(launch_id: str, root: Path | None = None) -> str:
    """The one-step unpublish, shell-quoted, runnable from any directory when
    ``root`` is given."""
    return studio_command(["launch", "unpublish", "--launch", launch_id], root)


def register_launch(
    conn: sqlite3.Connection,
    manifest: dict[str, Any],
    product_path: Path,
    bundle_dir: Path,
    notes: list[str] | None = None,
) -> str:
    """Record an assembled bundle. ``notes`` are OPERATOR-ONLY (local URLs,
    tool errors with paths): kept here, never in a published file."""
    record = manifest["record"]
    conn.execute(
        "INSERT INTO launches (id, product_slug, product_path, bundle_dir, record_sha256, status, "
        "created_at, operator_notes) VALUES (?,?,?,?,?, 'assembled', ?, ?)",
        (
            manifest["launch_id"],
            manifest["product"]["slug"],
            str(product_path.resolve()),
            str(bundle_dir.resolve()),
            record.get("content_sha256"),
            manifest["created_at"],
            json.dumps(notes or []),
        ),
    )
    conn.commit()
    return str(manifest["launch_id"])


def _launch_row(conn: sqlite3.Connection, launch_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM launches WHERE id = ?", (launch_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no launch '{launch_id}'")
    return row


# ---- gates -----------------------------------------------------------------------


def _gate(name: str, subject: str, failures: list[str], ok: str) -> GateResult:
    if failures:
        return GateResult(name, subject, "fail", "; ".join(failures))
    return GateResult(name, subject, "pass", ok)


def gate_record(
    bundle: Path, manifest: dict[str, Any], product: LaunchProduct | None = None
) -> list[GateResult]:
    """D1 + D3 over the bundled record.

    * ``d1_record`` — a record is present, and it is a frozen PUBLIC snapshot
      with a version (an authed live document or a local file is a ``launch
      build`` preview only);
    * ``d3_allowlist`` — the document passes qontinui-web's validator (ported),
      its digest matches, it is THIS product's (public slug == document slug ==
      product slug; document repos ⊆ product repos)."""
    subject = f"bundle {manifest.get('launch_id')}"
    record = manifest.get("record") or {}
    if not record.get("present"):
        return [
            GateResult(
                "d1_record",
                subject,
                "fail",
                f"no build record ({record.get('kind')}: {record.get('detail')}); "
                "decoration never stands in for it",
            )
        ]
    if record.get("source") != "public" or not isinstance(record.get("version"), int):
        return [
            GateResult(
                "d1_record",
                subject,
                "fail",
                f"the bundle was built from a {record.get('source')} record "
                f"(version {record.get('version')}); a launch publishes only from the public "
                "snapshot, so what readers see is what anyone can check",
            )
        ]
    out = [
        GateResult(
            "d1_record",
            subject,
            "pass",
            f"public snapshot v{record.get('version')} from {record.get('url')}",
        )
    ]
    path = bundle / "build-record.json"
    try:
        wrapped = json.loads(path.read_text(encoding="utf-8"))
        document = wrapped["document"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [*out, GateResult("d3_allowlist", subject, "fail", f"unreadable {path.name}: {e}")]
    problems = allowlist_violations(document)
    if isinstance(document, dict) and canonical_sha256(document) != record.get("content_sha256"):
        problems.append("build-record.json digest differs from the manifest's content_sha256")
    if not problems and product is not None:
        doc_product = document.get("product") or {}
        if doc_product.get("slug") != product.slug:
            problems.append(
                f"record is for product {doc_product.get('slug')!r}, not {product.slug!r}"
            )
        if record.get("public_slug") != product.slug:
            problems.append(
                f"public snapshot is published as {record.get('public_slug')!r}, "
                f"not {product.slug!r}"
            )
        declared = {r.lower() for r in product.repos}
        if any(str(r).lower() not in declared for r in doc_product.get("repos") or []):
            problems.append("record lists repos the product does not declare")
        excluded = excluded_names(document, product.repos)
        if len(excluded) > MAX_EXCLUDED_REPOS:
            problems.append(f"the product declares more than {MAX_EXCLUDED_REPOS} excluded repos")
        elif excluded:
            problems += allowlist_violations(document, excluded_names=excluded)
    out.append(
        _gate(
            "d3_allowlist",
            subject,
            problems,
            "passes the build-record/1 validator and is this product's record",
        )
    )
    return out


def gate_integrity(bundle: Path, manifest: dict[str, Any]) -> GateResult:
    files: dict[str, str] = manifest.get("files") or {}
    problems = [
        f"{rel} has a hidden (dot-prefixed) component" for rel in files if hidden_component(rel)
    ]
    for rel, digest in files.items():
        path = bundle / rel
        if not path.is_file():
            problems.append(f"{rel} missing")
        elif sha256_file(path) != digest:
            problems.append(f"{rel} changed since assembly")
    present = {
        rel
        for rel in (str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file())
        if rel != MANIFEST and not _studio_file(rel)
    }
    problems += [f"{rel} is not in the manifest" for rel in sorted(present - set(files))]
    return _gate(
        "integrity",
        f"bundle {manifest.get('launch_id')}",
        problems,
        f"{len(files)} file(s) as assembled",
    )


def gate_decor(
    bundle: Path, manifest: dict[str, Any], library: Library, product: LaunchProduct
) -> list[GateResult]:
    out: list[GateResult] = []
    for image in manifest.get("images") or []:
        if image.get("kind") != "decorative":
            continue
        rel = str(image.get("path"))
        subject = f"{rel} ({image.get('asset_uid')})"
        try:
            asset = library.asset(str(image.get("asset_uid")))
        except KeyError:
            out.append(
                GateResult("d5_provenance", subject, "fail", "no library record for this asset")
            )
            continue
        rejected = library.rejection(asset.uid)
        if rejected is not None:
            # The bundle was assembled before the rejection, or edited after.
            out.append(
                GateResult(
                    "library_approval",
                    subject,
                    "fail",
                    f"{rejected}; re-run the launch kit to assemble a bundle without it",
                )
            )
            continue
        digest = sha256_file(bundle / rel) if (bundle / rel).is_file() else None
        variant = next((v for v in library.variants(asset.uid) if v.sha256 == digest), None)
        if variant is None:
            out.append(
                GateResult(
                    "d5_provenance", subject, "fail", "file is not a recorded variant of the asset"
                )
            )
            continue
        out.append(d5_provenance(asset, variant))
        out.append(
            licence_verdict(asset, subject, product.publish.commercial, f"launch '{product.slug}'")
        )
    return out


_FIGURE = re.compile(
    r"<figure class='(?P<cls>[^']*)' data-ai-generated='(?P<ai>true|false)'>(?P<body>.*?)</figure>",
    re.S,
)
_IMG = re.compile(r"<img src='(?P<src>[^']*)'")
_CAPTION = re.compile(r"<figcaption>(?P<cap>.*?)</figcaption>", re.S)


def gate_d6(bundle: Path, manifest: dict[str, Any]) -> GateResult:
    kinds = {str(i.get("path")): i for i in manifest.get("images") or []}
    problems: list[str] = []
    for page in PAGES:
        text = (bundle / page).read_text(encoding="utf-8") if (bundle / page).is_file() else ""
        figured: set[str] = set()
        for fig in _FIGURE.finditer(text):
            img = _IMG.search(fig["body"])
            if img is None:
                continue
            src = html.unescape(img["src"]).split("?", 1)[0]
            figured.add(src)
            entry = kinds.get(src)
            if entry is None:
                problems.append(f"{page}: {src} is not a bundle image")
                continue
            cap = _CAPTION.search(fig["body"])
            caption = html.unescape(cap["cap"]) if cap else ""
            if fig["cls"] == "screenshot":
                if entry.get("kind") != "screenshot" or entry.get("ai_generated") is not False:
                    problems.append(
                        f"{page}: {src} shown as a screenshot but is {entry.get('kind')}"
                    )
            elif entry.get("ai_generated") is True:
                label = str(entry.get("label") or "")
                if fig["ai"] != "true" or not label.strip() or label not in caption:
                    problems.append(f"{page}: AI image {src} is not shown with its label '{label}'")
        for m in _IMG.finditer(text):
            src = html.unescape(m["src"]).split("?", 1)[0]
            entry = kinds.get(src)
            if entry is not None and entry.get("ai_generated") is True and src not in figured:
                problems.append(f"{page}: AI image {src} appears outside a labelled figure")
    return _gate(
        "d6",
        f"bundle {manifest.get('launch_id')}",
        problems,
        "screenshots are real; AI imagery labelled",
    )


def gate_footer(bundle: Path, manifest: dict[str, Any]) -> GateResult:
    url = (manifest.get("record") or {}).get("public_url")
    problems = []
    if not url:
        problems.append("no public build-record URL to link")
    else:
        anchor = f'<a class="factory" href="{html.escape(url, quote=True)}">{FOOTER_TEXT}</a>'
        for page in PAGES:
            path = bundle / page
            if not path.is_file() or anchor not in path.read_text(encoding="utf-8"):
                problems.append(f"{page}: footer does not link '{FOOTER_TEXT}' to {url}")
    return _gate(
        "footer", f"bundle {manifest.get('launch_id')}", problems, f"every page links {url}"
    )


def _studio_file(rel: str) -> bool:
    """The studio's own bookkeeping in a publish dir: the lock and journals,
    which live at its TOP level only (a same-named file deeper down is not
    the studio's)."""
    if Path(rel).parent != Path("."):
        return False
    return rel == LOCK_NAME or (rel.startswith(JOURNAL_PREFIX) and rel.endswith(".json"))


#: How :func:`_dir_contents` names the directory itself when the directory
#: itself is foreign (a symlink, or not a directory at all).
DIR_ITSELF = "(the directory itself)"


def _raise_walk_error(error: OSError) -> None:
    raise error


def _dir_contents(directory: Path) -> tuple[dict[str, Path], list[str]]:
    """``({rel: path} of regular files, [rel of everything else])`` under
    ``directory``, the studio's top-level lock and journals aside.

    Walked without following links. Anything that is not a regular file or a
    real directory — a symlink to a file or a directory (dangling or not), a
    FIFO, a socket, a device — is reported in the second list as itself:
    never hashed (a FIFO would block forever), never followed, never skipped,
    because a launch only ever places regular files. A symlinked (or
    non-directory) ``directory`` is reported as :data:`DIR_ITSELF`. A
    directory that cannot be read raises its :class:`OSError` rather than
    being silently left out."""
    files: dict[str, Path] = {}
    other: list[str] = []
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        return files, [DIR_ITSELF]
    if not directory.exists():
        return files, other
    for dirpath, dirnames, filenames in os.walk(
        directory, followlinks=False, onerror=_raise_walk_error
    ):
        here = Path(dirpath)
        for name in [*dirnames, *filenames]:
            path = here / name
            rel = str(path.relative_to(directory))
            if path.is_symlink():
                other.append(rel)
            elif name in dirnames:
                continue  # a real directory: walked
            elif not path.is_file():
                other.append(rel)
            elif not _studio_file(rel):
                files[rel] = path
    return files, sorted(other)


def _live_launch(conn: sqlite3.Connection, publish_dir: Path) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM launches WHERE status = 'published' AND publish_dir = ?",
        (str(publish_dir),),
    ).fetchone()
    return row


def gate_foreign_file(conn: sqlite3.Connection, publish_dir: Path, launch_id: str) -> GateResult:
    """Never clobber: every file in the publish dir must be one the live launch
    placed, still holding the bytes it placed (the lock and journals aside)."""
    live = _live_launch(conn, publish_dir)
    owned: dict[str, str] = json.loads(live["published_files"] or "{}") if live else {}
    subject = f"launch {launch_id} -> {publish_dir}"
    try:
        files, other = _dir_contents(publish_dir)
        # Non-regular entries first: they are named in the detail's example.
        foreign = other + sorted(
            rel for rel, path in files.items() if owned.get(rel) != sha256_file(path)
        )
    except OSError as e:
        return GateResult(
            "foreign_file",
            subject,
            "fail",
            f"the directory cannot be fully read ({type(e).__name__}: {e}); the kit never "
            "publishes over files it cannot see, so it refuses",
        )
    if foreign:
        return GateResult(
            "foreign_file",
            subject,
            "fail",
            f"{len(foreign)} file(s) there are not the live launch's bytes (e.g. {foreign[0]}); "
            "the kit never overwrites a file it did not place, so it refuses",
        )
    return GateResult("foreign_file", subject, "pass", "directory holds only the live launch")


def gate_already_live(conn: sqlite3.Connection, launch_id: str) -> GateResult:
    row = _launch_row(conn, launch_id)
    if row["status"] == "published":
        return GateResult(
            "already_live", f"launch {launch_id}", "fail", "this launch is already published"
        )
    return GateResult("already_live", f"launch {launch_id}", "pass", f"status {row['status']}")


#: Published text a reader can open (pages, the record, posts, the manifest).
_TEXT_SUFFIXES = (".html", ".json", ".txt", ".css", ".md", ".xml", ".svg")
#: An absolute operator path: a POSIX path under a home/system root, or a
#: Windows drive path. (Public URLs carry paths too, but never these roots.)
_ABS_PATH_RE = re.compile(
    r"(?<![a-z0-9._~/-])/(?:home|users|root|tmp|var|etc|opt|srv|mnt|private|run|media)/"
    # A drive path with either separator (D:\x, D:\\x, D:/x, D://x); the
    # lookbehind keeps a URL scheme (https://, file:/) from matching.
    r"|(?<![a-z0-9])[a-z]:[\\/]{1,2}[a-z0-9]",
    re.IGNORECASE,
)
#: Machine-internal names: loopback, link-local and private-zone hostnames,
#: and file: URLs. Case-insensitive, like hostnames and URL schemes are.
_INTERNAL_HOST_RE = re.compile(
    r"\blocalhost\b"
    r"|\b[a-z0-9-]+\.(?:local|internal|lan|corp|home\.arpa)\b"
    r"|\bfile:/",
    re.IGNORECASE,
)


#: Candidate IP literals; each is PARSED (ipaddress), never judged by shape.
#: A dotted quad may end a sentence ("on 127.0.0.1.") but not sit inside a
#: longer dotted number ("v1.2.3.4.5").
_IPV4_RE = re.compile(r"(?<![0-9])(?<![0-9]\.)(\d{1,3}(?:\.\d{1,3}){3})(?![0-9])(?!\.[0-9])")
#: A host in URL or host:port position, in any form a resolver accepts
#: (``127.1``, ``0x7f.1``) — inet_aton's grammar, judged only there.
_SHORT_V4_HOST_RE = re.compile(
    r"(?://|@)([0-9a-fx.]{1,20})(?=[:/\]'\"?#\s&<>,;)]|$)|\b([0-9a-fx.]{1,20}):\d{2,5}\b"
)
_PORT_SUFFIX_RE = re.compile(r":\d{1,5}$")
_BRACKETED_RE = re.compile(r"\[([0-9a-f:.%]{2,64})\]")
#: Runs of IPv6-literal characters; each run (one leading ':' allowed, as in
#: ``addr:fe80::1``) is a candidate.
_V6_RUN_RE = re.compile(r"[0-9a-f:.%]{2,64}")
#: The four internal families, and nothing wider (``is_private`` also covers
#: documentation and other special-purpose ranges that public text may use).
_INTERNAL_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or any(ip.version == net.version and ip in net for net in _INTERNAL_NETS)
    )


def _v4(literal: str) -> ipaddress.IPv4Address | None:
    """A dotted quad, leading zeros per octet dropped (``127.000.000.001``)."""
    parts = literal.split(".")
    if len(parts) != 4 or not all(p.isdigit() and int(p) <= 255 for p in parts):
        return None
    return ipaddress.IPv4Address(".".join(str(int(p)) for p in parts))


def _v4_host(literal: str) -> ipaddress.IPv4Address | None:
    """Any IPv4 spelling inet_aton accepts (``127.1``, ``0x7f.1``)."""
    literal = literal.rstrip(".")  # a sentence-ending dot ("Try http://127.1.")
    # A dot is required: a bare number before a colon is a time or a ratio
    # ("0:30", "4:3"), not a host.
    if not literal or not literal[0].isdigit() or "." not in literal.strip("."):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(literal))
    except OSError:
        return None


def _v6(literal: str, bracketed: bool) -> ipaddress.IPv6Address | None:
    literal = literal.split("%", 1)[0].rstrip(".")
    if literal.startswith(":") and not literal.startswith("::"):
        literal = literal[1:]  # "addr:fe80::1" -> "fe80::1"
    elif literal.startswith(":::"):
        literal = literal[1:]  # "ip:::1" -> "::1"
    if literal.count(":") < 2:
        return None
    # A bare "::" (CSS ::selection, Haskell ::) is not an address; it counts
    # only in brackets.
    if not bracketed and not any(c in "0123456789abcdef" for c in literal):
        return None
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        return None
    return ip if isinstance(ip, ipaddress.IPv6Address) else None


def _internal_ip(scan: str) -> str | None:
    """The first IP literal in ``scan`` that is loopback, link-local,
    unspecified, RFC 1918 or IPv6 unique-local (fc00::/7) — in any spelling a
    parser accepts — or None."""
    for m in _IPV4_RE.finditer(scan):
        ip4 = _v4(m.group(1))
        # x.y.z.0 (other than 0.0.0.0) is a network address, never a host one
        # could reach — and it is what version strings look like
        # ("Chrome/127.0.0.0", "Version=10.0.0.0").
        if ip4 is not None and ip4.packed[3] == 0 and not ip4.is_unspecified:
            continue
        if ip4 is not None and _is_internal(ip4):
            return m.group(1)
    for m in _SHORT_V4_HOST_RE.finditer(scan):
        literal = m.group(1) or m.group(2) or ""
        host = _v4_host(literal)
        if host is not None and _is_internal(host):
            return literal
    for regex, bracketed in ((_BRACKETED_RE, True), (_V6_RUN_RE, False)):
        for m in regex.finditer(scan):
            literal = m.group(1) if bracketed else m.group(0)
            ip6 = _v6(literal, bracketed)
            if ip6 is not None and _is_internal(ip6):
                return m.group(0)
            if not bracketed and "::" in literal:
                # "ECONNREFUSED ::1:3000": an unbracketed literal followed by
                # :port still parses (as ::1:3000, not loopback) — judge it
                # with the trailing port removed too.
                unported = _PORT_SUFFIX_RE.sub("", literal.rstrip("."))
                if unported != literal.rstrip("."):
                    ip6 = _v6(unported, bracketed)
                    if ip6 is not None and _is_internal(ip6):
                        return m.group(0)
    return None


def _scan_form(text: str) -> str:
    """What every leak pattern reads: NFKC (fullwidth and other compatibility
    spellings fold to ASCII) then casefold (``LOCALHOST``, ``Localhost``,
    ``FILE://``, ``C:\\USERS`` and a home path in another case all match)."""
    return unicodedata.normalize("NFKC", text).casefold()


def _bundle_outcome(bundle: Path, manifest: dict[str, Any]) -> FetchOutcome:
    """The record (or its absence) the bundle was assembled from, re-read and
    re-validated from the bundle itself."""
    record = manifest.get("record") or {}
    if not record.get("present"):
        return NoBuildRecord(
            str(record.get("kind") or "not_found"),  # type: ignore[arg-type]
            "",
            str(record.get("detail") or ""),
        )
    wrapped = json.loads((bundle / "build-record.json").read_text(encoding="utf-8"))
    document = wrapped["document"]
    version = wrapped.get("version")
    return FetchedRecord(
        parse_document(document),
        document,
        wrapped.get("source", "file"),
        wrapped.get("url") or "",
        canonical_sha256(document),
        version if isinstance(version, int) else None,
        wrapped.get("published_at"),
        wrapped.get("public_slug"),
    )


def _scan_text(rel: str, text: str, home: str, hostname: str) -> list[str]:
    """Every leak in ``text``, read case- and compatibility-insensitively
    (:func:`_scan_form`): the home directory, an absolute local path, a
    machine-internal host or ``file:`` URL, this machine's hostname."""
    problems: list[str] = []
    scan = _scan_form(text)
    home_form = _scan_form(home) if home else ""
    if home_form and home_form in scan:
        problems.append(f"{rel}: contains the operator's home directory")
    elif _ABS_PATH_RE.search(scan):
        problems.append(f"{rel}: contains an absolute local path")
    if _INTERNAL_HOST_RE.search(scan) or _internal_ip(scan):
        problems.append(f"{rel}: names a machine-internal host")
    else:
        host_form = _scan_form(hostname)
        if (
            len(host_form) >= 3
            and host_form != "localhost"
            and re.search(rf"(?<![a-z0-9-]){re.escape(host_form)}(?![a-z0-9-])", scan)
        ):
            problems.append(f"{rel}: names this machine ({hostname!r})")
    return problems


def gate_public_text(bundle: Path, manifest: dict[str, Any], product: LaunchProduct) -> GateResult:
    """No text the KIT composes from local inputs may carry an absolute
    operator path, the home directory, this machine's hostname, or a
    machine-internal host name — the things a tool error, a capture URL or a
    product field drags into a page.

    Pages are judged by POSITION, never by matching content: the gate re-runs
    the one page renderer (:func:`bundle.render_pages`) on the bundle's own
    inputs — first to prove the bundle's pages are exactly that render, then
    with every record string replaced by a placeholder — and scans the
    placeholder render, so a record string can neither be refused for what
    the factory's D3 rules accept nor hide a leak in kit-composed text. The
    manifest, posts and film.json are scanned as written (no masking);
    ``build-record.json`` is the record itself, judged by the D3 validator."""
    subject = f"bundle {manifest.get('launch_id')}"
    files: dict[str, str] = manifest.get("files") or {}
    home = str(Path.home())
    hostname = socket.gethostname().lower()
    images = list(manifest.get("images") or [])
    film = manifest.get("film")
    video_rel = film.get("path") if isinstance(film, dict) else None
    problems: list[str] = []
    try:
        outcome = _bundle_outcome(bundle, manifest)
        rendered = render_pages(bundle, product, outcome, images, video_rel)
        placeholder = render_pages(
            bundle,
            product,
            redacted(outcome) if isinstance(outcome, FetchedRecord) else outcome,
            images,
            video_rel,
        )
    except (OSError, ValueError, KeyError, TypeError, StudioError) as e:
        return GateResult("public_text", subject, "fail", f"cannot re-render the pages: {e}")
    for page, text in rendered.items():
        try:
            if (bundle / page).read_text(encoding="utf-8") != text:
                problems.append(f"{page}: is not the kit's render of this bundle's inputs")
        except OSError:
            problems.append(f"{page}: missing")
    for page, text in placeholder.items():
        problems += _scan_text(page, text, home, hostname)
    for rel in sorted([*files, MANIFEST]):
        if not rel.endswith(_TEXT_SUFFIXES) or rel in rendered or rel == "build-record.json":
            continue
        try:
            text = (bundle / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        problems += _scan_text(rel, text, home, hostname)
    return _gate(
        "public_text",
        subject,
        problems,
        "no local paths or internal host names in any text the kit composed",
    )


def pre_publication_gates(
    bundle: Path, manifest: dict[str, Any], library: Library, product: LaunchProduct
) -> list[GateResult]:
    return [
        *gate_record(bundle, manifest, product),
        gate_integrity(bundle, manifest),
        gate_public_text(bundle, manifest, product),
        *gate_decor(bundle, manifest, library, product),
        gate_d6(bundle, manifest),
        gate_footer(bundle, manifest),
    ]


# ---- the launch journal ---------------------------------------------------------------

LaunchJournalKind = Literal["publish", "unpublish"]
_HEX32 = r"[0-9a-f]{32}"
_SHA = re.compile(r"[0-9a-f]{64}")


def _journal_path(publish_dir: Path, launch_id: str) -> Path:
    return publish_dir / f"{JOURNAL_PREFIX}{launch_id}.json"


@dataclass
class LaunchJournal:
    """Everything one launch publish or unpublish does to its live directory.

    The same protocol as the pipeline's :class:`~qontinui_studio.publish.Journal`
    (staged -> placed, originals -> backups, each original re-checked right
    before it moves) for a bundle, whose files nest in subdirectories. Paths are
    stored RELATIVE to the publish dir, and :meth:`read` validates every one."""

    launch_id: str
    publish_dir: Path
    kind: LaunchJournalKind = "publish"
    # (staged rel, dest rel, sha256 of the bytes placed at dest)
    placed: list[tuple[str, str, str]] = field(default_factory=list)
    moved: list[Moved] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return _journal_path(self.publish_dir, self.launch_id)

    def _abs(self, rel: str) -> Path:
        return self.publish_dir / rel

    def write(self) -> None:
        payload = {
            "kind": self.kind,
            "launch_id": self.launch_id,
            "placed": [list(p) for p in self.placed],
            "moved": [
                [
                    str(m.original.relative_to(self.publish_dir)),
                    str(m.backup.relative_to(self.publish_dir)),
                    m.sha,
                    m.owned,
                ]
                for m in self.moved
            ],
        }
        atomic_write(self.path, json.dumps(payload, indent=2).encode())

    @classmethod
    def read(cls, path: Path, publish_dir: Path, conn: sqlite3.Connection) -> LaunchJournal:
        """Load and VALIDATE a journal found in ``publish_dir``: the launch id
        matches the file name and is a launch of this directory, the kind is
        known, and every path is a bundle path (or its staged/prev sibling)
        inside ``publish_dir``. Any deviation raises :class:`JournalError`."""

        def bad(why: str) -> JournalError:
            return JournalError(f"{publish_dir}: refusing journal {path.name}: {why}")

        m = re.fullmatch(re.escape(JOURNAL_PREFIX) + r"(.+)\.json", path.name)
        if m is None:
            raise bad("not a journal file name")
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise bad(f"unreadable ({e})") from e
        if not isinstance(p, dict):
            raise bad("not a JSON object")
        launch_id = p.get("launch_id")
        if launch_id != m.group(1):
            raise bad("launch_id does not match the file name")
        row = conn.execute("SELECT publish_dir FROM launches WHERE id = ?", (launch_id,)).fetchone()
        if row is None or row["publish_dir"] != str(publish_dir):
            raise bad(f"no launch {launch_id!r} of this directory")
        if p.get("kind") not in ("publish", "unpublish"):
            raise bad(f"unknown kind {p.get('kind')!r}")
        live = publish_dir.resolve()

        def bundle_path(raw: object) -> str:
            rel = str(raw)
            target = (publish_dir / rel).resolve()
            if not target.is_relative_to(live) or target == live or _studio_file(rel):
                raise bad(f"{rel} is not a bundle path inside the publish dir")
            if any(part.startswith(".") for part in Path(rel).parts):
                raise bad(f"{rel} is not a bundle path (hidden component)")
            return rel

        def sibling(raw: object, dest: str, tag: str) -> str:
            rel = Path(str(raw))
            d = Path(dest)
            if rel.parent != d.parent or not re.fullmatch(
                re.escape(f".{d.name}.{tag}-") + _HEX32, rel.name
            ):
                raise bad(f"{rel} is not a {tag} file of {dest}")
            return str(rel)

        def sha(raw: object) -> str:
            if not isinstance(raw, str) or not _SHA.fullmatch(raw):
                raise bad("bad sha256")
            return raw

        try:
            placed = []
            for staged, dest, digest in p["placed"]:
                d = bundle_path(dest)
                placed.append((sibling(staged, d, "staged"), d, sha(digest)))
            moved = []
            for original, backup, digest, owned in p["moved"]:
                o = bundle_path(original)
                b = sibling(backup, o, "prev")
                moved.append(Moved(publish_dir / o, publish_dir / b, sha(digest), bool(owned)))
        except JournalError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise bad(f"malformed ({type(e).__name__}: {e})") from e
        return cls(launch_id, publish_dir, p["kind"], placed, moved)

    def apply(self) -> None:
        """Perform the journalled change. Each original is re-checked right
        before it moves; a destination that appeared since planning aborts."""
        for m in self.moved:
            if _file_sha(m.original) != m.sha:
                raise ForeignFile(m.original, "changed after it was checked; refusing to move it")
            os.replace(m.original, m.backup)
        for staged, dest, _ in self.placed:
            if self._abs(dest).exists():
                raise ForeignFile(
                    self._abs(dest), "appeared after it was checked; refusing to overwrite it"
                )
            os.replace(self._abs(staged), self._abs(dest))

    def roll_forward(self) -> list[str]:
        """Drop staging leftovers and backups; a backup that is not a studio
        publication's bytes is KEPT and reported."""
        kept: list[str] = []
        for staged, _, _ in self.placed:
            self._abs(staged).unlink(missing_ok=True)
        for m in self.moved:
            if not m.backup.exists():
                continue
            if m.owned and _file_sha(m.backup) == m.sha:
                m.backup.unlink()
            else:
                kept.append(f"kept {m.backup} (not a launch-kit publication)")
        self.path.unlink(missing_ok=True)
        return kept

    def roll_back(self) -> list[str]:
        """Undo this change on disk. Returns what could NOT be restored."""
        problems: list[str] = []
        for staged, dest, digest in self.placed:
            if self._abs(staged).exists():
                self._abs(staged).unlink()
                continue
            if _file_sha(self._abs(dest)) == digest:
                self._abs(dest).unlink()
        for m in reversed(self.moved):
            if not m.backup.exists():
                continue
            if m.original.exists():
                problems.append(f"{m.original} holds unknown bytes; previous kept at {m.backup}")
                continue
            m.original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(m.backup, m.original)
        self.path.unlink(missing_ok=True)
        return problems


def _file_sha(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


# ---- publish / unpublish ------------------------------------------------------------------


def _record_gates(
    conn: sqlite3.Connection, launch_id: str, results: list[GateResult], now: datetime
) -> None:
    conn.executemany(
        "INSERT INTO launch_gate_results (launch_id, gate, subject, outcome, detail, evaluated_at) "
        "VALUES (?,?,?,?,?,?)",
        [(launch_id, g.gate, g.subject, g.outcome, g.detail, now.isoformat()) for g in results],
    )


def _journal_committed(conn: sqlite3.Connection, journal: LaunchJournal) -> bool:
    row = conn.execute("SELECT status FROM launches WHERE id = ?", (journal.launch_id,)).fetchone()
    if row is None:
        return False
    if journal.kind == "publish":
        return bool(row["status"] == "published")
    return bool(row["status"] == "unpublished")


def _locked_dirs(product_or_dir: LaunchProduct | Path) -> list[Path]:
    if isinstance(product_or_dir, Path):
        return [product_or_dir]
    target = product_or_dir.publish
    return [target.publish_dir] + ([target.preview_dir] if target.preview_dir else [])


def _recover_locked(
    conn: sqlite3.Connection, publish_dir: Path, notifier: Notifier, root: Path | None
) -> list[str]:
    """Bring one (already locked) launch directory back to a consistent state:
    replay leftover journals (validated first), refuse launches orphaned in
    ``publishing``, and send the published notice a live launch never got.
    A journal that fails validation raises :class:`JournalError`; nothing in it
    is acted on."""
    report: list[str] = []
    if publish_dir.is_dir():
        for path in sorted(publish_dir.glob(f"{JOURNAL_PREFIX}*.json")):
            journal = LaunchJournal.read(path, publish_dir, conn)
            if _journal_committed(conn, journal):
                kept = journal.roll_forward()
                report.append(
                    f"completed interrupted {journal.kind} of launch {journal.launch_id}"
                    + (f" ({'; '.join(kept)})" if kept else "")
                )
                continue
            problems = journal.roll_back()
            if journal.kind == "publish":
                conn.execute(
                    "UPDATE launches SET status = 'refused', reason = ? "
                    "WHERE id = ? AND status = 'publishing'",
                    ("interrupted publish rolled back", journal.launch_id),
                )
                conn.commit()
            line = f"rolled back interrupted {journal.kind} of launch {journal.launch_id}" + (
                f" ({'; '.join(problems)})" if problems else ""
            )
            report.append(line)
            notifier.notify(
                Notice(
                    batch_id=journal.launch_id,
                    outcome=f"{journal.kind}-rolled-back",
                    title=f"qontinui-studio rolled back an interrupted {journal.kind} of launch "
                    f"{journal.launch_id}",
                    body=line,
                )
            )
    for row in conn.execute(
        "SELECT id FROM launches WHERE status = 'publishing' AND publish_dir = ?",
        (str(publish_dir),),
    ).fetchall():
        conn.execute(
            "UPDATE launches SET status = 'refused', reason = ? WHERE id = ?",
            ("publish interrupted before any live change", row["id"]),
        )
        conn.commit()
        report.append(f"launch {row['id']}: interrupted publish marked refused")
        notifier.notify(
            Notice(
                batch_id=row["id"],
                outcome="refused",
                title=f"qontinui-studio: publish of launch {row['id']} was interrupted",
                body="The publish was interrupted before any live change; the launch is "
                "refused and can be published again.",
            )
        )
    for row in conn.execute(
        "SELECT * FROM launches WHERE status = 'published' AND publish_dir = ? "
        "AND notified_at IS NULL",
        (str(publish_dir),),
    ).fetchall():
        notifier.notify(_published_notice(row, root))
        _mark_notified(conn, row["id"])
        report.append(f"launch {row['id']}: sent the missing published notice")
    return report


def recover_launches(
    conn: sqlite3.Connection, publish_dir: Path, notifier: Notifier, root: Path | None = None
) -> list[str]:
    """Recover one launch directory (holds its lock)."""
    with site_locks([publish_dir]):
        return _recover_locked(conn, publish_dir, notifier, root)


def recover_all_launches(
    conn: sqlite3.Connection, notifier: Notifier, root: Path | None = None
) -> tuple[list[str], dict[str, str]]:
    """Recover every directory a launch was ever published to, each isolated:
    returns (report lines, {publish_dir: why its recovery failed})."""
    lines: list[str] = []
    failed: dict[str, str] = {}
    dirs = {
        r["publish_dir"]
        for r in conn.execute(
            "SELECT DISTINCT publish_dir FROM launches WHERE publish_dir IS NOT NULL"
        )
    }
    for d in sorted(dirs):
        if not Path(d).is_dir():
            continue
        try:
            lines += recover_launches(conn, Path(d), notifier, root)
        except Exception as e:
            conn.rollback()
            failed[d] = f"{type(e).__name__}: {e}"
    return lines, failed


def _mark_notified(conn: sqlite3.Connection, launch_id: str) -> None:
    conn.execute(
        "UPDATE launches SET notified_at = datetime('now') WHERE id = ? AND notified_at IS NULL",
        (launch_id,),
    )
    conn.commit()


def _published_notice(
    row: sqlite3.Row, root: Path | None, extra: list[str] | None = None
) -> Notice:
    """The published notice, from the launch row alone: the product's
    ``publish.unpublish_command`` and ``publish.public_url`` were frozen on the
    row when it went live (schema v5), so a notice re-sent later (recovery,
    readopt) says what the publish said, whatever yaml sits at its path now.
    The command is printed verbatim in place of the studio's own; with a
    public URL the notice names the public pages, not the local files."""
    publish_dir = Path(row["publish_dir"])
    files = json.loads(row["published_files"] or "{}")
    base = (row["public_url"] or "").rstrip("/")
    return Notice(
        batch_id=row["id"],
        outcome="published",
        title=f"qontinui-studio launched {row['product_slug']} ({row['id']})",
        body="\n".join(
            [
                f"Launch bundle published to {publish_dir}: {len(files)} file(s); every gate "
                "passed on the preview before the live swap.",
                *(extra or []),
            ]
        ),
        urls=[f"{base}/{p}" if base else str(publish_dir / p) for p in PAGES],
        unpublish_command=row["unpublish_command"] or unpublish_command(row["id"], root),
    )


def _write_preview(bundle: Path, files: list[str], preview_dir: Path) -> bytes:
    """Write this bundle into the preview and return the manifest bytes the
    preview must then serve. Only files the previous studio-written preview
    manifest lists are deleted — never anything else in the preview dir."""
    previous = preview_dir / MANIFEST
    if previous.is_file():
        try:
            old = json.loads(previous.read_text(encoding="utf-8"))
        except ValueError:
            old = None
        if isinstance(old, dict) and old.get("schema") == BUNDLE_SCHEMA:
            for rel in old.get("files") or {}:
                path = (preview_dir / str(rel)).resolve()
                if path.is_relative_to(preview_dir.resolve()) and path.is_file():
                    path.unlink()
    for rel in files:
        atomic_copy(bundle / rel, preview_dir / rel)
    return (preview_dir / MANIFEST).read_bytes()


def preview_target(product: LaunchProduct, launch_id: str, attempt: str) -> str:
    """The launch's preview URL for one D7 attempt: the launch id AND a fresh
    attempt token ride in the query string, so the bridged page's own URL
    proves it loaded THIS attempt of THIS launch."""
    assert product.publish.preview_url is not None
    return with_query(
        product.publish.preview_url, {"studio_launch": launch_id, "studio_attempt": attempt}
    )


#: The query parameters that bind a bridged page to one launch D7 attempt.
LAUNCH_PARAMS = ("studio_launch", "studio_attempt")


def image_groups(
    bundle: Path, manifest: dict[str, Any]
) -> tuple[dict[str, dict[str, str]], set[str]]:
    """D7's image check over EVERY bundle image: ``({group: {rel: sha256}},
    unrendered groups)``. A group is :func:`bundle.media_group` of the image;
    an image the landing page shows must be registered under its
    ``data-media-id``, render from the preview origin as ``<rel>?sha=<sha256>``
    and load; one it does not show (the social cards) is fetched beside the
    preview manifest and sha-compared."""
    text = (bundle / "index.html").read_text(encoding="utf-8")
    shown = {html.unescape(m["src"]).split("?", 1)[0] for m in _IMG.finditer(text)}
    groups: dict[str, dict[str, str]] = {}
    unrendered: set[str] = set()
    for image in manifest.get("images") or []:
        rel = str(image.get("path"))
        group = media_group(rel)
        if group in groups:
            raise BundleRefused(f"two bundle images share D7 media group {group}")
        groups[group] = {rel: str(image.get("sha256"))}
        if rel not in shown:
            unrendered.add(group)
    return groups, unrendered


def publish_launch(
    conn: sqlite3.Connection,
    launch_id: str,
    product: LaunchProduct,
    notifier: Notifier,
    clock: Callable[[], datetime],
    bridge: BridgeFactory = HttpUiBridge,
    fetch: ManifestFetch = http_manifest_fetch,
    root: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    d7_deadline_s: float = 30.0,
    records: BuildRecordClient | None = None,
) -> LaunchPublishResult:
    """Publish an assembled launch. Holds the locks of the publish dir, the
    preview dir AND the bundle dir from the first gate to the commit, so the
    bundle the gates judge cannot be rebuilt underneath them. ``records`` is
    the factory door used to re-check the record (default: the product's)."""
    records = records or default_records_client(product)
    bundle_dir = Path(_launch_row(conn, launch_id)["bundle_dir"])
    with site_locks([*_locked_dirs(product), bundle_dir]):
        _recover_locked(conn, product.publish.publish_dir, notifier, root)
        return _publish_locked(
            conn,
            launch_id,
            product,
            notifier,
            clock,
            bridge,
            fetch,
            root,
            sleep,
            d7_deadline_s,
            records,
        )


def default_records_client(product: LaunchProduct) -> BuildRecordClient:
    return BuildRecordClient(web_base=product.record.web_base)


def gate_current_record(
    manifest: dict[str, Any], product: LaunchProduct, records: BuildRecordClient
) -> GateResult:
    """The record must STILL be published, as the very snapshot the bundle was
    built from: a retracted record (public door 404) or a newer/different
    snapshot refuses — the bundle is rebuilt from the current one instead."""
    subject = f"bundle {manifest.get('launch_id')}"
    record = manifest.get("record") or {}
    try:
        current = records.fetch_public(product.slug)
    except BuildRecordInvalid as e:
        return GateResult(
            "d1_current", subject, "fail", f"the public door answers an invalid record: {e}"
        )
    if isinstance(current, NoBuildRecord):
        return GateResult(
            "d1_current",
            subject,
            "fail",
            f"the public record is no longer available ({current.kind}: {current.detail}); "
            "a retracted record is never launched",
        )
    if current.version != record.get("version") or current.content_sha256 != record.get(
        "content_sha256"
    ):
        return GateResult(
            "d1_current",
            subject,
            "fail",
            f"the public snapshot is now v{current.version} ({current.content_sha256[:12]}), "
            f"not the v{record.get('version')} this bundle was built from; rebuild it",
        )
    return GateResult(
        "d1_current",
        subject,
        "pass",
        f"public snapshot v{current.version} is still the current one",
    )


def gate_tenant(
    manifest: dict[str, Any], product: LaunchProduct, records: BuildRecordClient
) -> GateResult:
    """The public snapshot under this slug must be the OPERATOR's: public slugs
    are first-publisher-owned, product slugs per-tenant, so another tenant can
    own the public snapshot of a slug the operator also defined and opted in.

    Proven either by the authed ``/publication`` answer — the caller's tenant
    owns it, it is not unpublished, and its version and content_sha256 are the
    ones this bundle was built from — or, when that route is unavailable (404:
    not deployed; no bearer configured), by the product's pin
    (``record.expected_sha256`` and/or ``record.expected_version``). A tenant's
    product listing is never sufficient."""
    subject = f"bundle {manifest.get('launch_id')}"
    record = manifest.get("record") or {}
    try:
        publication = records.publication(product.slug)
    except BuildRecordInvalid as e:
        return GateResult("d1_tenant", subject, "fail", f"unreadable publication answer: {e}")
    if not isinstance(publication, NoBuildRecord):
        if not publication.owned_by_caller_tenant:
            return GateResult(
                "d1_tenant",
                subject,
                "fail",
                f"the public snapshot of '{product.slug}' is owned by another tenant; it is "
                "never launched as the operator's",
            )
        if publication.unpublished:
            return GateResult(
                "d1_tenant", subject, "fail", "the operator's publication is unpublished"
            )
        if publication.version != record.get("version") or publication.content_sha256 != record.get(
            "content_sha256"
        ):
            return GateResult(
                "d1_tenant",
                subject,
                "fail",
                f"the operator's publication is v{publication.version}, not the snapshot "
                f"v{record.get('version')} this bundle was built from",
            )
        return GateResult(
            "d1_tenant",
            subject,
            "pass",
            f"the operator's tenant owns public snapshot v{publication.version}",
        )
    if publication.kind not in ("not_found", "no_credential", "refused_door"):
        return GateResult(
            "d1_tenant",
            subject,
            "fail",
            f"cannot read who owns the snapshot ({publication.kind}: {publication.detail})",
        )
    pins = {
        "content_sha256": product.record.expected_sha256,
        "version": product.record.expected_version,
    }
    if not any(v is not None for v in pins.values()):
        return GateResult(
            "d1_tenant",
            subject,
            "fail",
            f"cannot prove the snapshot is this tenant's: the publication door is unavailable "
            f"({publication.kind}: {publication.detail}) and products/{product.slug}.yaml pins "
            "no record.expected_sha256 or record.expected_version",
        )
    wrong = [k for k, v in pins.items() if v is not None and record.get(k) != v]
    if wrong:
        return GateResult(
            "d1_tenant",
            subject,
            "fail",
            f"the snapshot does not match the pinned {', '.join(wrong)}",
        )
    return GateResult("d1_tenant", subject, "pass", "the snapshot matches the product's pin")


def _publish_locked(
    conn: sqlite3.Connection,
    launch_id: str,
    product: LaunchProduct,
    notifier: Notifier,
    clock: Callable[[], datetime],
    bridge: BridgeFactory,
    fetch: ManifestFetch,
    root: Path | None,
    sleep: Callable[[float], None],
    d7_deadline_s: float,
    records: BuildRecordClient,
) -> LaunchPublishResult:
    now = clock()
    row = _launch_row(conn, launch_id)
    bundle = Path(row["bundle_dir"])
    publish_dir = product.publish.publish_dir
    subject = f"launch {launch_id}"
    result = LaunchPublishResult(launch_id=launch_id, published=False)
    try:
        manifest_bytes = (bundle / MANIFEST).read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError) as e:
        result.gate_results.append(
            GateResult("integrity", f"bundle {launch_id}", "fail", f"no readable manifest: {e}")
        )
        return _refuse(conn, notifier, result, now, "the bundle cannot be read")
    if manifest.get("launch_id") != launch_id:
        result.gate_results.append(
            GateResult(
                "integrity",
                f"bundle {launch_id}",
                "fail",
                f"manifest is launch {manifest.get('launch_id')}",
            )
        )
    result.gate_results.append(gate_already_live(conn, launch_id))
    result.gate_results += pre_publication_gates(bundle, manifest, Library(conn), product)
    result.gate_results.append(gate_foreign_file(conn, publish_dir, launch_id))
    if not result.failures:
        result.gate_results.append(gate_current_record(manifest, product, records))
        result.gate_results.append(gate_tenant(manifest, product, records))
    target = product.publish
    if not target.has_preview:
        # D7 is required, with no waiver.
        result.gate_results.append(
            page_health_gate(
                subject, None, missing="no UI Bridge preview declared; D7 is required — refused"
            )
        )
    if result.failures:
        return _refuse(conn, notifier, result, now, "pre-publication gates failed")
    assert target.preview_dir is not None and target.ui_bridge_url is not None

    # ---- D7 on a preview, before anything live changes ---------------------------
    conn.execute(
        "UPDATE launches SET status = 'publishing', reason = NULL, publish_dir = ? WHERE id = ?",
        (str(publish_dir), launch_id),
    )
    conn.commit()
    files = [*manifest["files"], MANIFEST]
    try:
        served = _write_preview(bundle, files, target.preview_dir)
    except OSError as e:
        result.gate_results.append(
            GateResult("d7_preview", subject, "fail", f"preview could not be written: {e}")
        )
        return _refuse(conn, notifier, result, now, "D7 preview failed")
    expected = hashlib.sha256(served).hexdigest()
    if expected != hashlib.sha256(manifest_bytes).hexdigest():
        result.gate_results.append(
            GateResult(
                "integrity",
                subject,
                "fail",
                "the bundle's manifest changed after the gates read it; nothing went live",
            )
        )
        return _refuse(conn, notifier, result, now, "the bundle changed during publication")
    preview = preview_manifest_gate(
        subject, target.preview_manifest_url, expected, fetch, "this bundle's"
    )
    result.gate_results.append(preview)
    if not preview.failed:
        assert target.preview_url is not None
        try:
            groups, unrendered = image_groups(bundle, manifest)
        except BundleRefused as e:
            result.gate_results.append(GateResult("d7_images", subject, "fail", str(e)))
            return _refuse(conn, notifier, result, now, "D7 cannot bind the bundle's images")
        # The asset pipeline's Waiter: D7 polls in the caller's clock and sleep.
        waiter = Waiter.of(clock, sleep)
        spec = PreviewSpec(
            subject=subject,
            preview_url=target.preview_url,
            preview_manifest_url=target.preview_manifest_url,
            unrendered=frozenset(unrendered),
            params=LAUNCH_PARAMS,
        )
        result.gate_results += preview_page_gates(
            spec,
            target.ui_bridge_url,
            preview_target(product, launch_id, uuid.uuid4().hex),
            groups,
            bridge,
            fetch,
            what="this launch's preview",
            deadline_s=d7_deadline_s,
            sleep=waiter.sleep,
            monotonic=waiter.monotonic,
        )
    if result.failures:
        return _refuse(conn, notifier, result, now, "D7 failed on the preview; nothing went live")

    # ---- journalled swap; the DB transaction (gate results included) commits it --
    previous = _live_launch(conn, publish_dir)
    journal = LaunchJournal(launch_id=launch_id, publish_dir=publish_dir)
    copied: dict[str, str] = {}
    staged_paths: list[Path] = []
    try:
        publish_dir.mkdir(parents=True, exist_ok=True)
        to_move: set[str] = set()
        for rel in files:
            dest = publish_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            staged = dest.with_name(f".{dest.name}.staged-{uuid.uuid4().hex}")
            staged_paths.append(staged)
            shutil.copyfile(bundle / rel, staged)
            sha = sha256_file(staged)
            # Only the bytes the gates judged go live: each file against the
            # manifest D7 verified, the manifest against the served bytes.
            want = expected if rel == MANIFEST else manifest["files"].get(rel)
            if sha != want:
                raise BundleChanged(rel)
            journal.placed.append((str(staged.relative_to(publish_dir)), rel, sha))
            copied[rel] = sha
            to_move.add(rel)
        prev_files: dict[str, str] = (
            json.loads(previous["published_files"] or "{}") if previous is not None else {}
        )
        to_move.update(prev_files)
        for rel in sorted(to_move):
            original = publish_dir / rel
            if original.exists():
                journal.moved.append(
                    Moved(
                        original,
                        original.with_name(f".{original.name}.prev-{uuid.uuid4().hex}"),
                        sha256_file(original),
                        prev_files.get(rel) == sha256_file(original),
                    )
                )
        journal.write()
        journal.apply()
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        _record_gates(conn, launch_id, result.gate_results, now)
        conn.execute(
            "UPDATE launches SET status = 'unpublished', unpublished_at = ?, superseded_by = ? "
            "WHERE status = 'published' AND publish_dir = ? AND id != ?",
            (now.isoformat(), launch_id, str(publish_dir), launch_id),
        )
        conn.execute(
            "UPDATE launches SET status = 'published', reason = NULL, publish_dir = ?, "
            "published_files = ?, published_at = ?, unpublished_at = NULL, superseded_by = NULL, "
            "notified_at = NULL, unpublish_command = ?, public_url = ? WHERE id = ?",
            (
                str(publish_dir),
                json.dumps(copied),
                now.isoformat(),
                product.publish.unpublish_command,
                product.publish.public_url,
                launch_id,
            ),
        )
        conn.commit()
    except BundleChanged as e:
        conn.rollback()
        if journal.path.exists():
            journal.roll_back()
        for staged in staged_paths:
            staged.unlink(missing_ok=True)
        result.gate_results.append(
            GateResult(
                "integrity",
                subject,
                "fail",
                f"{e.rel} is not the bytes the gates judged (sha256 mismatch); nothing went live",
            )
        )
        return _refuse(conn, notifier, result, now, "the bundle changed during publication")
    except ForeignFile as e:
        conn.rollback()
        problems = journal.roll_back() if journal.path.exists() else []
        for staged in staged_paths:
            staged.unlink(missing_ok=True)
        result.gate_results.append(
            GateResult(
                "foreign_file", subject, "fail", f"{e}" + (f"; {problems}" if problems else "")
            )
        )
        return _refuse(conn, notifier, result, now, "a live file changed under the publish")
    except BaseException:
        conn.rollback()
        if journal.path.exists():
            journal.roll_back()
        for staged in staged_paths:
            staged.unlink(missing_ok=True)
        conn.execute(
            "UPDATE launches SET status = 'refused', reason = ? WHERE id = ?",
            ("live swap failed and was rolled back", launch_id),
        )
        conn.commit()
        raise

    # ---- committed: from here on nothing is a refusal --------------------------
    result.published = True
    result.files = [publish_dir / rel for rel in copied]
    try:
        result.warnings += journal.roll_forward()
        _prune_empty_dirs(publish_dir)
    except Exception as e:  # post-commit cleanup: logged and left to recovery
        result.warnings.append(
            f"post-commit cleanup failed ({type(e).__name__}: {e}); "
            "the next publish/unpublish or `studio launch recover` finishes it"
        )
    health = next((g for g in result.gate_results if g.gate == "d7_page_health"), None)
    extra = [
        f"Build record: {manifest['record'].get('public_url')} "
        f"(sha256 {manifest['record'].get('content_sha256')})",
    ]
    if health is not None:
        extra.append(f"d7_page_health: {health.outcome} — {health.detail}")
    if previous is not None:
        extra.append(f"superseded launch {previous['id']}")
    extra += [f"warning: {w}" for w in result.warnings]
    extra += [f"note: {n}" for n in json.loads(row["operator_notes"] or "[]")]
    try:
        notice = _published_notice(_launch_row(conn, launch_id), root, extra)
        result.delivery = notifier.notify(notice)
        _mark_notified(conn, launch_id)
    except Exception as e:  # recovery re-sends a notice whose notified_at is unset
        result.warnings.append(f"published notice not sent ({type(e).__name__}: {e})")
    return result


def _prune_empty_dirs(root: Path) -> None:
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        if not any(d.iterdir()):
            d.rmdir()


def _refuse(
    conn: sqlite3.Connection,
    notifier: Notifier,
    result: LaunchPublishResult,
    now: datetime,
    why: str,
) -> LaunchPublishResult:
    conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    _record_gates(conn, result.launch_id, result.gate_results, now)
    conn.execute(
        "UPDATE launches SET status = 'refused', reason = ? WHERE id = ? AND status != 'published'",
        (why, result.launch_id),
    )
    conn.commit()
    lines = [why + ":"] + [f"- {g.gate} {g.subject}: {g.detail}" for g in result.failures]
    result.delivery = notifier.notify(
        Notice(
            batch_id=result.launch_id,
            outcome="refused",
            title=f"qontinui-studio refused launch {result.launch_id}",
            body="\n".join(lines),
        )
    )
    return result


def unpublish_launch(
    conn: sqlite3.Connection,
    launch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
    root: Path | None = None,
) -> list[Path]:
    """The one-step unpublish: takes the live launch down, journalled and under
    the directory's lock. Only files still holding the bytes this launch placed
    are removed.

    Nothing is restored automatically. Bringing an earlier launch back is
    ``studio launch publish --launch <id>``, which re-runs EVERY gate — D7 on a
    preview included — so a restore is always re-gated."""
    row = _launch_row(conn, launch_id)
    if row["status"] != "published" or not row["publish_dir"]:
        # Never touch a directory a later launch now owns (a superseded
        # launch's files may share bytes with its successor's).
        why = f"superseded by {row['superseded_by']}" if row["superseded_by"] else row["status"]
        raise LaunchNotPublished(f"launch '{launch_id}' is not published ({why}); nothing removed")
    publish_dir = Path(row["publish_dir"])
    with site_locks([publish_dir]):
        _recover_locked(conn, publish_dir, notifier, root)
        row = _launch_row(conn, launch_id)
        if row["status"] != "published":
            raise LaunchNotPublished(f"launch '{launch_id}' is no longer published")
        now = clock()
        journal = LaunchJournal(launch_id=launch_id, publish_dir=publish_dir, kind="unpublish")
        removed: list[Path] = []
        for rel, digest in json.loads(row["published_files"] or "{}").items():
            path = publish_dir / rel
            if path.is_file() and sha256_file(path) == digest:
                journal.moved.append(
                    Moved(
                        path, path.with_name(f".{path.name}.prev-{uuid.uuid4().hex}"), digest, True
                    )
                )
                removed.append(path)
        try:
            journal.write()
            journal.apply()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE launches SET status = 'unpublished', unpublished_at = ? WHERE id = ?",
                (now.isoformat(), launch_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            journal.roll_back()
            raise
        notes: list[str] = []
        try:
            notes += journal.roll_forward()
            _prune_empty_dirs(publish_dir)
        except Exception as e:  # committed; cleanup is left to recovery
            notes.append(f"post-commit cleanup failed ({type(e).__name__}: {e})")
        notifier.notify(
            Notice(
                batch_id=launch_id,
                outcome="unpublished",
                title=f"qontinui-studio unpublished launch {launch_id}",
                body="\n".join([f"Removed {len(removed)} file(s) from {publish_dir}", *notes]),
            )
        )
    return removed


# ---- readopt ----------------------------------------------------------------------

ReadoptArm = Literal["superseded", "by_bytes"]
ReadoptRefusalCode = Literal[
    "not_unpublished",
    "no_publish_dir",
    "launch_live",
    "not_newest",
    "bytes_differ",
    "foreign_file",
]


class ReadoptRefused(StudioError):
    """``readopt`` refused: the launch cannot be made live again as it stands.
    ``code`` says which condition failed."""

    def __init__(self, launch_id: str, code: ReadoptRefusalCode, detail: str) -> None:
        super().__init__(f"launch '{launch_id}' not readopted ({code}): {detail}")
        self.launch_id = launch_id
        self.code: ReadoptRefusalCode = code
        self.detail = detail


@dataclass
class ReadoptResult:
    launch_id: str
    arm: ReadoptArm
    publish_dir: Path
    files: int
    delivery: Delivery | None = None
    # Post-commit problems (the notice): the launch IS published again.
    warnings: list[str] = field(default_factory=list)


def _dir_matches_launch(publish_dir: Path, owned: dict[str, str]) -> tuple[list[str], list[str]]:
    """``(differ, foreign)``: files the launch placed that are missing or hold
    other bytes, and files the launch did not place (every symlink among
    them). The studio's top-level lock and journals are ignored — exactly what
    ``gate_foreign_file`` ignores, walked the same way."""
    present, other = _dir_contents(publish_dir)
    differ = sorted(
        rel
        for rel, digest in owned.items()
        if rel not in present or sha256_file(present[rel]) != digest
    )
    foreign = other + sorted(rel for rel in present if rel not in owned)
    return differ, foreign


def readopt_launch(
    conn: sqlite3.Connection,
    launch_id: str,
    notifier: Notifier,
    clock: Callable[[], datetime],
    root: Path | None = None,
) -> ReadoptResult:
    """Make an ``unpublished`` launch the live publication of its directory
    again, when that directory already holds exactly its bytes — the state a
    hub clone is left in when a successor's (or a retraction's) PR closes
    unlanded and the clone is reset to the landed tree.

    Two arms, both refusing while ANY launch at the directory is published and
    both requiring the directory to hold exactly the launch's
    ``published_files`` by sha256 and nothing else (the lock and journals
    aside):

    * ``superseded`` — the launch was superseded (``superseded_by`` is set);
    * ``by_bytes`` — the launch was unpublished outright (no ``superseded_by``)
      and is the newest launch that ever went live at that directory.

    Under the directory's lock, after recovery, in one transaction: the launch
    becomes ``published`` again with ``superseded_by`` and ``unpublished_at``
    cleared. Nothing on disk is touched. Refusals raise
    :class:`ReadoptRefused`; an unknown launch raises :class:`KeyError`."""
    row = _launch_row(conn, launch_id)
    if not row["publish_dir"]:
        raise ReadoptRefused(
            launch_id, "no_publish_dir", f"status {row['status']}; it never went live anywhere"
        )
    publish_dir = Path(row["publish_dir"])
    # Checked before the lock, which would create (or follow) the directory.
    if publish_dir.is_symlink() or (publish_dir.exists() and not publish_dir.is_dir()):
        raise ReadoptRefused(
            launch_id,
            "foreign_file",
            f"{publish_dir} is not a real directory ({DIR_ITSELF} is a symlink or a file); "
            "a launch only ever placed regular files in a real directory",
        )
    if not publish_dir.exists():
        raise ReadoptRefused(
            launch_id,
            "bytes_differ",
            f"{publish_dir} does not exist; it holds none of this launch's files",
        )
    with site_locks([publish_dir]):
        _recover_locked(conn, publish_dir, notifier, root)
        now = clock()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _launch_row(conn, launch_id)
            if row["status"] != "unpublished":
                raise ReadoptRefused(
                    launch_id,
                    "not_unpublished",
                    f"status is {row['status']}; only an unpublished launch is readopted",
                )
            live = _live_launch(conn, publish_dir)
            if live is not None:
                raise ReadoptRefused(
                    launch_id,
                    "launch_live",
                    f"launch {live['id']} is the live publication of {publish_dir}",
                )
            arm: ReadoptArm = "superseded" if row["superseded_by"] else "by_bytes"
            if arm == "by_bytes":
                newest = conn.execute(
                    "SELECT id FROM launches WHERE publish_dir = ? AND published_at IS NOT NULL "
                    "ORDER BY published_at DESC, created_at DESC, id DESC LIMIT 1",
                    (str(publish_dir),),
                ).fetchone()
                if newest is None or newest["id"] != launch_id:
                    raise ReadoptRefused(
                        launch_id,
                        "not_newest",
                        f"launch {newest['id'] if newest else '(none)'} went live at "
                        f"{publish_dir} after it; only the newest is readopted by its bytes",
                    )
            owned: dict[str, str] = json.loads(row["published_files"] or "{}")
            differ, foreign = _dir_matches_launch(publish_dir, owned)
            if foreign:
                raise ReadoptRefused(
                    launch_id,
                    "foreign_file",
                    f"{len(foreign)} file(s) in {publish_dir} are not this launch's "
                    f"(e.g. {foreign[0]})",
                )
            if differ or not owned:
                raise ReadoptRefused(
                    launch_id,
                    "bytes_differ",
                    f"{len(differ)} of this launch's file(s) are missing or hold other bytes "
                    f"(e.g. {differ[0]})"
                    if differ
                    else "the launch records no published files",
                )
            # published_at is "when it last went live": a readopted launch is
            # the newest live one at the dir, so a later by-bytes readopt of it
            # is not refused as not_newest because of the successor it outlived.
            conn.execute(
                "UPDATE launches SET status = 'published', reason = NULL, superseded_by = NULL, "
                "unpublished_at = NULL, published_at = ? WHERE id = ? AND status = 'unpublished'",
                (now.isoformat(), launch_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        result = ReadoptResult(
            launch_id=launch_id, arm=arm, publish_dir=publish_dir, files=len(owned)
        )
        try:
            row = _launch_row(conn, launch_id)
            notice = _published_notice(
                row,
                root,
                [
                    f"Readopted ({arm}) at {now.isoformat()}: the directory already held exactly "
                    "this launch's bytes; nothing on disk changed."
                ],
            )
            result.delivery = notifier.notify(
                dataclasses.replace(
                    notice,
                    outcome="readopted",
                    title=f"qontinui-studio readopted launch {launch_id} ({row['product_slug']})",
                )
            )
        except Exception as e:  # committed; the notice is best-effort
            result.warnings.append(f"readopted notice not sent ({type(e).__name__}: {e})")
    return result
