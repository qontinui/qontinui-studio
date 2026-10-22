"""The factory's build record (plan Phase 1), as the launch kit reads it.

Two doors serve a ``build-record/1`` document:

* the PUBLIC snapshot, ``GET {web}/api/v1/public/build-records/{slug}`` —
  unauthenticated, the frozen version a tenant published, wrapped as
  ``{public_slug, version, published_at, content_sha256, document}``;
* the AUTHED proxy, ``GET {web}/api/v1/build-records/{slug}`` — the live
  document from coord, bare, behind a bearer (``STUDIO_WEB_BEARER`` /
  ``STUDIO_WEB_BEARER_FILE``). With ``STUDIO_WEB_ACTIVE_TENANT`` set, every
  authed call also sends ``X-Qontinui-Active-Tenant`` so it acts in that tenant.

Whichever door answers, the document is re-validated here against the same D3
allowlist the factory applies (key for key, at every depth; any extra key is a
refusal), so a factory regression cannot reach a public launch page through
the kit either.

A 404 or an unreachable door is a typed :class:`NoBuildRecord` outcome — never
a crash, and never an empty record standing in for one. ``unknowns`` are
carried through: a null field is rendered as ``unknown — <reason>``, never 0.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qontinui_studio.errors import StudioError, omitted
from qontinui_studio.launch.allowlist import BUILD_RECORD_ALLOWED_PATHS, build_record_violations
from qontinui_studio.notify import resolve_bearer

SCHEMA: Final = "build-record/1"
WEB_BASE: Final = "https://api.qontinui.io"
PUBLIC_PATH: Final = "/api/v1/public/build-records/{slug}"
AUTHED_PATH: Final = "/api/v1/build-records/{slug}"
PUBLICATION_PATH: Final = "/api/v1/build-records/{slug}/publication"
WEB_BEARER_ENV: Final = "STUDIO_WEB_BEARER"
WEB_BEARER_FILE_ENV: Final = "STUDIO_WEB_BEARER_FILE"
#: The tenant an authed web call acts in (a UUID). qontinui-web reads it from
#: ``X-Qontinui-Active-Tenant``; without it a multi-tenant account's call lands
#: in its home tenant. The hub sets it so every read is in the publishing tenant.
ACTIVE_TENANT_ENV: Final = "STUDIO_WEB_ACTIVE_TENANT"
ACTIVE_TENANT_HEADER: Final = "X-Qontinui-Active-Tenant"
_UUID_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
SLUG_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"


class BuildRecordInvalid(StudioError):
    """The document failed the D3 allowlist or the build-record/1 schema.
    ``violations`` names every failure. Such a document is never rendered."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("build record refused: " + "; ".join(violations))
        self.violations = violations


# ---- the D3 allowlist: qontinui-web's validator, ported ---------------------

#: Every allowed key as a dotted path (``prs[].repo``) — the flat form a test pins.
ALLOWED_PATHS: Final[frozenset[str]] = BUILD_RECORD_ALLOWED_PATHS


def allowlist_violations(document: Any, excluded_names: Iterable[str] = ()) -> list[str]:
    """Every way ``document`` breaks the D3 allowlist (qontinui-web's validator,
    ported in :mod:`qontinui_studio.launch.allowlist`): unlisted keys, per-slot
    value types, the ``<path>: <reason_code>`` unknown grammar, and the content
    scan (private repo names, UUIDs, emails, hidden/lookalike characters).
    Empty when the document may be rendered."""
    return build_record_violations(document, excluded_names=excluded_names)


#: Human wording for coord's reason codes (``UNKNOWN_REASON_CODES``).
REASON_PHRASES: Final[dict[str, str]] = {
    "not_established": "the factory could not establish it",
    "door_unavailable": "the factory's reader for it was unavailable",
    "excluded_not_known_public": "withheld: not known to be public",
    "census_provisional": "the session census is still provisional",
    "not_applicable": "not applicable",
}
_INDEX: Final = re.compile(r"\[\d+\]")


def reason_text(code: str) -> str:
    return f"{REASON_PHRASES.get(code, code)} ({code})"


@dataclass(frozen=True)
class ListStatus:
    """How far a list in the record can be counted.

    ``known`` — the list is complete; ``lower_bound`` — coord says it could not
    establish the whole list (``not_established``) but some of it is there;
    ``unknown`` — the list's reader failed, or nothing could be established."""

    state: Literal["known", "lower_bound", "unknown"]
    code: str | None = None


# ---- the typed document ------------------------------------------------------


class _Strict(BaseModel):
    # strict: a "2" where an int belongs is a refusal, never a coercion.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Product(_Strict):
    slug: str
    title: str
    repos: list[str]
    window_start: str | None = None
    window_end: str | None = None


class WorkUnit(_Strict):
    slug: str
    title: str | None = None
    status: str
    vetted_at: str | None = None
    shipped_at: str | None = None


class TimelineEntry(_Strict):
    at: str
    work_unit: str
    from_status: str | None = None
    to_status: str


class PullRequest(_Strict):
    repo: str
    number: int
    title: str | None = None
    opened_at: str | None = None
    landed_at: str | None = None
    ci_duration_secs: int | None = None
    attempts: int | None = None


class Gates(_Strict):
    # The contract says int; coord's composer emits null when the gate reader
    # failed, and that null is an UNKNOWN, never a 0.
    registered: int | None = None
    cleared: int | None = None


class Sessions(_Strict):
    count: int | None = None
    unknown_reason: str | None = None


class SelfCorrections(_Strict):
    red_ci_fixed: int | None = None
    review_findings_fixed: int | None = None
    gate_reopens: int | None = None


class BuildRecord(_Strict):
    """A ``build-record/1`` document. Every field the kit renders comes from here."""

    schema_: Literal["build-record/1"]
    generated_at: str
    product: Product
    work_units: list[WorkUnit]
    timeline: list[TimelineEntry]
    prs: list[PullRequest]
    gates: Gates
    sessions: Sessions
    wall_clock_secs: int | None = None
    self_corrections: SelfCorrections
    unknowns: list[str]

    def unknown_codes(self, path: str) -> list[str]:
        """The reason codes the record gives for ``path`` exactly (``prs[0].title``
        and ``prs[].title`` both count for ``prs[].title``). coord writes each
        ``unknowns`` entry as ``<path>: <reason_code>``; the validator has
        already refused any other form."""
        want = _INDEX.sub("[]", path)
        entries = list(self.unknowns)
        if self.sessions.unknown_reason:
            entries.append(self.sessions.unknown_reason)
        codes: list[str] = []
        for entry in entries:
            where, sep, code = entry.partition(": ")
            if sep and _INDEX.sub("[]", where) == want and code not in codes:
                codes.append(code)
        return codes

    def unknown_reason(self, path: str) -> str:
        """Why the (null) field at ``path`` is unknown, in words plus the code."""
        codes = self.unknown_codes(path)
        if not codes:
            return "the record leaves it null and gives no reason code"
        return "; ".join(reason_text(c) for c in codes)

    def list_status(self, path: str) -> ListStatus:
        """Whether a count over the list at ``path`` (``work_units``, ``prs``,
        ``timeline``) is known, a lower bound, or unknown — from the record's
        own codes, never guessed from wording."""
        codes = self.unknown_codes(path)
        hard = [c for c in codes if c != "not_established"]
        if hard:
            return ListStatus("unknown", hard[0])
        if codes:
            items = getattr(self, path)
            if items:
                return ListStatus("lower_bound", "not_established")
            return ListStatus("unknown", "not_established")
        return ListStatus("known")


def parse_document(document: Any) -> BuildRecord:
    """Validate a raw document against D3 + build-record/1 and type it."""
    violations = allowlist_violations(document)
    if violations:
        raise BuildRecordInvalid(violations)
    data = dict(document)
    data["schema_"] = data.pop("schema")
    try:
        return BuildRecord.model_validate(data)
    except ValidationError as e:
        raise BuildRecordInvalid(
            [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        ) from e


def canonical_sha256(document: dict[str, Any]) -> str:
    """qontinui-web's ``content_sha256``: sorted keys, no whitespace, UTF-8."""
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- fetch outcomes -----------------------------------------------------------

Source = Literal["public", "authed", "file"]


@dataclass(frozen=True)
class FetchedRecord:
    record: BuildRecord
    document: dict[str, Any]
    source: Source
    url: str
    content_sha256: str
    version: int | None = None
    published_at: str | None = None
    public_slug: str | None = None

    @property
    def publishable(self) -> bool:
        """Only a frozen PUBLIC snapshot (with a version) may back a published
        launch. An authed live document or a local file is for ``launch build``
        previews only — what a reader is shown must be what anyone can check."""
        return self.source == "public" and self.version is not None

    def public_url(self) -> str | None:
        """The URL that may appear in published files: the public door's, never
        an authed or local one."""
        return self.url if self.source == "public" else None


#: Most repos a product may define that the record left out (qontinui-web's
#: ``MAX_EXCLUDED_REPOS``): each excluded name is scanned for in every string.
MAX_EXCLUDED_REPOS: Final = 50


def excluded_names(document: Any, repos: list[str]) -> set[str]:
    """Names of repos the product DEFINES but the record left out (coord could
    not establish them as public) — qontinui-web's ``_excluded_names``."""
    defined = {r.lower() for r in repos}
    doc_product = document.get("product") if isinstance(document, dict) else None
    doc_repos = doc_product.get("repos") if isinstance(doc_product, dict) else None
    present = (
        {r.lower() for r in doc_repos if isinstance(r, str)}
        if isinstance(doc_repos, list)
        else set()
    )
    return {r.split("/", 1)[1] for r in defined - present if "/" in r and r.split("/", 1)[1]}


#: What every record string becomes in the placeholder render.
RECORD_PLACEHOLDER: Final = "record-value"


def redacted(fetched: FetchedRecord) -> FetchedRecord:
    """The same record with EVERY string value replaced by
    :data:`RECORD_PLACEHOLDER` (the schema id aside): the input to the
    placeholder render the public_text gate scans."""

    def walk(value: Any, key: str | None) -> Any:
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if isinstance(value, str) and key != "schema":
            return RECORD_PLACEHOLDER
        return value

    document = walk(fetched.document, None)
    data = dict(document)
    data["schema_"] = data.pop("schema")
    record = BuildRecord.model_validate(data)
    return dataclasses.replace(fetched, record=record, document=document)


def identity_violations(fetched: FetchedRecord, slug: str, repos: list[str]) -> list[str]:
    """The record must be THIS product's: ``public_slug`` (when the public door
    served one) == ``document.product.slug`` == the product's slug, every repo
    the record lists is one the product declares, and no string names a repo
    the product declares but the record left out (not known public)."""
    out: list[str] = []
    doc_slug = fetched.record.product.slug
    if doc_slug != slug:
        out.append(f"record is for product {doc_slug!r}, not {slug!r}")
    if fetched.source == "public" and fetched.public_slug != slug:
        out.append(f"public snapshot is published as {fetched.public_slug!r}, not {slug!r}")
    declared = {r.lower() for r in repos}
    extra = [r for r in fetched.record.product.repos if r.lower() not in declared]
    if extra:
        out.append(f"record lists {len(extra)} repo(s) the product does not declare")
    excluded = excluded_names(fetched.document, repos)
    if len(excluded) > MAX_EXCLUDED_REPOS:
        out.append(
            f"the product declares {len(excluded)} repos the record left out "
            f"(more than {MAX_EXCLUDED_REPOS})"
        )
    elif excluded:
        out += build_record_violations(fetched.document, excluded_names=excluded)
    return out


class PublicationAnswer(BaseModel):
    """``GET /api/v1/build-records/{slug}/publication`` exactly as qontinui-web
    serves it (``BuildRecordPublication``, f300da6): these five keys, no more."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    public_slug: str
    owned_by_caller_tenant: bool
    unpublished: bool
    version: int | None = Field(ge=1)
    content_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Publication:
    """The caller tenant's view of the public snapshot under a slug."""

    owned_by_caller_tenant: bool
    version: int | None
    content_sha256: str | None
    unpublished: bool


@dataclass(frozen=True)
class NoBuildRecord:
    """No usable build record — a typed outcome, never an empty record.

    ``kind``: ``not_found`` (404: nothing published under the slug yet, or
    Phase 1 not deployed), ``unreachable`` (transport failure or 5xx),
    ``unauthorized`` (401/403 on the authed door), ``no_credential`` (authed
    door asked for with no bearer configured), ``refused_door`` (the authed
    door's host is not https on an allowlisted host, so no bearer is sent),
    ``misconfigured`` (the authed door's configuration is wrong — e.g. a
    ``STUDIO_WEB_ACTIVE_TENANT`` that is not a tenant UUID; never a reason to
    fall back to anything)."""

    kind: Literal[
        "not_found", "unreachable", "unauthorized", "no_credential", "refused_door", "misconfigured"
    ]
    url: str
    detail: str

    def describe(self) -> str:
        return f"no build record yet ({self.kind}): {self.detail}"


FetchOutcome = FetchedRecord | NoBuildRecord


def public_record_url(slug: str, web_base: str = WEB_BASE) -> str:
    return web_base.rstrip("/") + PUBLIC_PATH.format(slug=slug)


#: Hosts a web bearer may be sent to (over https only).
BEARER_HOSTS: Final[frozenset[str]] = frozenset({"api.qontinui.io"})


class BuildRecordClient:
    def __init__(
        self,
        web_base: str = WEB_BASE,
        http: httpx.Client | None = None,
        bearer: str | None = None,
        bearer_hosts: frozenset[str] = BEARER_HOSTS,
    ) -> None:
        self.web_base = web_base.rstrip("/")
        self.http = http or httpx.Client(timeout=30.0)
        self.bearer = bearer
        self.bearer_hosts = bearer_hosts

    def _get(self, url: str, headers: dict[str, str]) -> httpx.Response | NoBuildRecord:
        try:
            resp = self.http.get(url, headers=headers)
        except httpx.HTTPError as e:
            return NoBuildRecord("unreachable", url, f"transport failure: {e}")
        if resp.status_code == 404:
            return NoBuildRecord("not_found", url, f"HTTP 404: {omitted(resp.text)}")
        if resp.status_code in (401, 403):
            return NoBuildRecord(
                "unauthorized", url, f"HTTP {resp.status_code}: {omitted(resp.text)}"
            )
        if resp.status_code >= 500:
            return NoBuildRecord(
                "unreachable", url, f"HTTP {resp.status_code}: {omitted(resp.text)}"
            )
        if resp.status_code != 200:
            raise BuildRecordInvalid(
                [f"{url} answered HTTP {resp.status_code}: {omitted(resp.text)}"]
            )
        return resp

    @staticmethod
    def _json(resp: httpx.Response, url: str) -> Any:
        try:
            return resp.json()
        except ValueError as e:
            raise BuildRecordInvalid([f"{url} answered a non-JSON body"]) from e

    def fetch_public(self, slug: str) -> FetchOutcome:
        url = public_record_url(slug, self.web_base)
        resp = self._get(url, {})
        if isinstance(resp, NoBuildRecord):
            return resp
        body = self._json(resp, url)
        return snapshot_from_wrapper(body, "public", url)

    def _bearer_headers(self, url: str) -> dict[str, str] | NoBuildRecord:
        """The Authorization header for an authed door — only over https to an
        allowlisted host, and only when a bearer is configured."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in self.bearer_hosts:
            return NoBuildRecord(
                "refused_door",
                url,
                f"a bearer is sent only over https to {sorted(self.bearer_hosts)}; "
                f"{parsed.scheme}://{host} is not one",
            )
        bearer = self.bearer or resolve_bearer(WEB_BEARER_ENV, WEB_BEARER_FILE_ENV)
        if not bearer:
            return NoBuildRecord(
                "no_credential", url, f"set {WEB_BEARER_ENV} or {WEB_BEARER_FILE_ENV}"
            )
        headers = {"Authorization": f"Bearer {bearer}"}
        tenant = os.environ.get(ACTIVE_TENANT_ENV, "").strip()
        if tenant:
            if not _UUID_RE.fullmatch(tenant):
                # Never act in an unintended tenant: a malformed pin is a
                # configuration error, never silently dropped (and never a
                # reason for a gate to fall back to the product's pins).
                return NoBuildRecord(
                    "misconfigured", url, f"{ACTIVE_TENANT_ENV} is set but is not a tenant UUID"
                )
            headers[ACTIVE_TENANT_HEADER] = tenant
        return headers

    def fetch_authed(self, slug: str) -> FetchOutcome:
        url = self.web_base + AUTHED_PATH.format(slug=slug)
        headers = self._bearer_headers(url)
        if isinstance(headers, NoBuildRecord):
            return headers
        resp = self._get(url, headers)
        if isinstance(resp, NoBuildRecord):
            return resp
        document = self._json(resp, url)
        record = parse_document(document)
        return FetchedRecord(record, document, "authed", url, canonical_sha256(document))

    def publication(self, slug: str) -> Publication | NoBuildRecord:
        """Who owns the PUBLIC snapshot under ``slug``, as the bearer's tenant
        sees it: ``GET /api/v1/build-records/{slug}/publication`` →
        ``{owned_by_caller_tenant, version, content_sha256, unpublished}``.

        Public slugs are first-publisher-owned while product slugs are
        per-tenant, so "the slug is public in my listing" proves nothing; only
        this answer (or a pin in the product definition) ties the public
        snapshot to the operator. A 404 means the route is not deployed (or
        the tenant never published) — a typed ``not_found``, never a pass."""
        url = self.web_base + PUBLICATION_PATH.format(slug=quote(slug, safe=""))
        headers = self._bearer_headers(url)
        if isinstance(headers, NoBuildRecord):
            return headers
        resp = self._get(url, headers)
        if isinstance(resp, NoBuildRecord):
            return resp
        body = self._json(resp, url)
        try:
            answer = PublicationAnswer.model_validate(body)
        except ValidationError as e:
            raise BuildRecordInvalid(
                [f"{url}: not the /publication shape ({e.error_count()} error(s))"]
            ) from e
        if answer.public_slug != slug:
            raise BuildRecordInvalid(
                [f"{url}: answered for {omitted(answer.public_slug)}, not {slug!r}"]
            )
        if not answer.owned_by_caller_tenant and (
            answer.unpublished or answer.version is not None or answer.content_sha256 is not None
        ):
            raise BuildRecordInvalid([f"{url}: a not-owned answer must carry no state"])
        owned, version, digest, unpublished = (
            answer.owned_by_caller_tenant,
            answer.version,
            answer.content_sha256,
            answer.unpublished,
        )
        return Publication(owned, version, digest, unpublished)

    def fetch(self, slug: str, source: Literal["public", "authed"]) -> FetchOutcome:
        return self.fetch_public(slug) if source == "public" else self.fetch_authed(slug)


def snapshot_from_wrapper(body: Any, source: Source, url: str) -> FetchedRecord:
    """Read the public route's ``{public_slug, version, published_at,
    content_sha256, document}`` wrapper, verifying the digest."""
    if not isinstance(body, dict) or not isinstance(body.get("document"), dict):
        raise BuildRecordInvalid([f"{url}: expected a snapshot wrapper with a 'document' object"])
    document: dict[str, Any] = body["document"]
    record = parse_document(document)
    digest = canonical_sha256(document)
    claimed = body.get("content_sha256")
    if claimed is not None and claimed != digest:
        raise BuildRecordInvalid(
            [f"content_sha256 {claimed} does not match the document's canonical digest {digest}"]
        )
    version = body.get("version")
    published_at = body.get("published_at")
    public_slug = body.get("public_slug")
    return FetchedRecord(
        record,
        document,
        source,
        url,
        digest,
        version if isinstance(version, int) and not isinstance(version, bool) else None,
        published_at if isinstance(published_at, str) else None,
        public_slug if isinstance(public_slug, str) else None,
    )


def load_record_file(path: Path) -> FetchedRecord:
    """A document (bare, or in the public wrapper) from a local JSON file."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BuildRecordInvalid([f"cannot read {path}: {e}"]) from e
    url = path.resolve().as_uri()
    if isinstance(body, dict) and "document" in body:
        return snapshot_from_wrapper(body, "file", url)
    record = parse_document(body)
    return FetchedRecord(record, body, "file", url, canonical_sha256(body))
