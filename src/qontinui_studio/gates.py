"""D5 / D6 / D7 publication gates as code.

Each gate is a pure function over the library records and the site config,
returning a :class:`GateResult`. A batch publishes only when no gate fails
(D7: automated gates decide publication; the operator is notified, not asked).

D3 (the build-record export allowlist) is enforced by the factory's export
(Phase 1, qontinui-coord + qontinui-web) and again by the launch kit before it
renders or publishes a bundle (``launch/allowlist.py``, ``launch/publish.py``).

The D7 functions come in two layers: a target-neutral core over a
:class:`PreviewSpec` (``page_health_gate``, ``preview_page_gates``,
``images_gate``, ``preview_images_settled``, ``preview_manifest_gate``),
shared by sites and launch bundles, and the site wrappers (``d7_*``,
``images_settled``) the asset pipeline calls — same rules, same messages.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx

from qontinui_studio import marker, provenance
from qontinui_studio.errors import omitted
from qontinui_studio.library import AssetRecord, VariantRecord
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.sites import Site, Slot

# ``not_configured`` is a FAILURE (D7: a gate that did not run did not pass).
Outcome = Literal["pass", "fail", "not_configured"]


@dataclass(frozen=True)
class GateResult:
    gate: str
    subject: str
    outcome: Outcome
    detail: str

    @property
    def failed(self) -> bool:
        return self.outcome in ("fail", "not_configured")


def _subject(asset: AssetRecord, variant: VariantRecord) -> str:
    return f"{asset.uid} -> {variant.site}/{variant.slot} ({variant.format})"


def d5_provenance(asset: AssetRecord, variant: VariantRecord) -> GateResult:
    """Every asset carries complete provenance, and the bytes are the bytes recorded."""
    subject = _subject(asset, variant)
    missing = [
        name
        for name in ("provider", "model", "prompt", "brief_id", "created_at", "sha256", "watermark")
        if not getattr(asset, name)
    ]
    if missing:
        return GateResult("d5_provenance", subject, "fail", f"provenance missing: {missing}")
    if not asset.ai_generated:
        return GateResult("d5_provenance", subject, "fail", "ai_generated is not true")
    for label, path, digest in (
        ("raw", Path(asset.raw_path), asset.sha256),
        ("variant", Path(variant.path), variant.sha256),
    ):
        if not path.is_file():
            return GateResult("d5_provenance", subject, "fail", f"{label} file {path} is missing")
        if sha256_file(path) != digest:
            return GateResult(
                "d5_provenance",
                subject,
                "fail",
                f"{label} file {path} does not match its recorded sha256",
            )
        if asset.origin != "generated":
            continue
        # Plan 2026-10-13 Phase 11 step 4 / a5ec7670: every generated file and
        # derivative carries the IPTC AI-generated marker and a complete
        # provenance sidecar describing exactly these bytes.
        if not marker.is_marked(path):
            return GateResult(
                "d5_provenance",
                subject,
                "fail",
                f"{label} file {path} lacks the AI-generated marker",
            )
        record = provenance.read(path)
        if record is None:
            return GateResult(
                "d5_provenance", subject, "fail", f"{label} file {path} has no provenance sidecar"
            )
        gaps = provenance.missing(record)
        if gaps:
            return GateResult(
                "d5_provenance",
                subject,
                "fail",
                f"{label} file {path}: provenance sidecar incomplete: {gaps}",
            )
        if record["file"]["sha256"] != digest:
            return GateResult(
                "d5_provenance",
                subject,
                "fail",
                f"{label} file {path}: provenance sidecar describes other bytes",
            )
    return GateResult(
        "d5_provenance", subject, "pass", "provenance complete; digests match; marker present"
    )


def d5_licence(asset: AssetRecord, variant: VariantRecord, site: Site) -> GateResult:
    """Refuse an unreviewed licence anywhere, and a non-commercial one on a commercial site."""
    return licence_verdict(asset, _subject(asset, variant), site.commercial, f"site '{site.id}'")


def licence_verdict(asset: AssetRecord, subject: str, commercial: bool, target: str) -> GateResult:
    """The D5 licence rule for any publication target (a site, a launch bundle):
    an unreviewed or unknown licence is refused everywhere, a non-commercial one
    on a commercial target."""
    if asset.licence_tos_reviewed is None:
        return GateResult(
            "d5_licence",
            subject,
            "fail",
            f"{asset.provider}: licence unreviewed (no tos_reviewed date)",
        )
    if asset.licence_commercial_use == "unknown":
        return GateResult(
            "d5_licence",
            subject,
            "fail",
            f"{asset.provider}: commercial terms unknown (unreviewed)",
        )
    if commercial and asset.licence_commercial_use != "yes":
        return GateResult(
            "d5_licence",
            subject,
            "fail",
            f"{asset.provider}: commercial_use is '{asset.licence_commercial_use}' "
            f"but {target} is commercial",
        )
    return GateResult(
        "d5_licence",
        subject,
        "pass",
        f"commercial_use={asset.licence_commercial_use} reviewed {asset.licence_tos_reviewed}",
    )


def d6_not_the_real_thing(
    asset: AssetRecord, variant: VariantRecord, site: Site, slot: Slot, label: str
) -> GateResult:
    """AI imagery is never presented as the real thing."""
    subject = _subject(asset, variant)
    if not asset.ai_generated:
        return GateResult("d6", subject, "fail", "asset is not flagged ai_generated")
    if slot.category == "product_screenshot":
        return GateResult(
            "d6",
            subject,
            "fail",
            "product screenshots come from the running product via UI Bridge, never a generator",
        )
    if site.kind == "real_business" and slot.category in ("food", "premises"):
        return GateResult(
            "d6",
            subject,
            "fail",
            f"real business '{site.id}': {slot.category} imagery must be real photos, not AI",
        )
    if site.kind == "demo" and "illustrative" not in label.casefold():
        return GateResult(
            "d6",
            subject,
            "fail",
            f"demo site '{site.id}' must label AI imagery as illustrative (label: '{label}')",
        )
    if site.kind != "demo" and not re.search(r"\bai\b", label, re.IGNORECASE):
        return GateResult(
            "d6",
            subject,
            "fail",
            f"site '{site.id}' must label AI imagery as AI (label: '{label}')",
        )
    return GateResult("d6", subject, "pass", f"labelled '{label}'")


PageHealthProbe = Callable[[str], dict[str, object]]
# Page-health summaries that publish. Anything else — CRITICAL, an unknown or
# new value — refuses.
ACCEPTED_SUMMARIES = ("OK", "WARNING")


def http_page_health_probe(url: str) -> dict[str, object]:
    """POST a UI Bridge page-health endpoint (knowledge-base doc
    ``page-health-diagnostic.md``) and return its JSON body."""
    resp = httpx.post(url, timeout=30.0)
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("page-health answered a non-object body")
    return body


def d7_page_health(site: Site, probe: PageHealthProbe = http_page_health_probe) -> GateResult:
    """A UI Bridge page-health pass: ``probe`` is POSTed the site's bridge
    page-health URL. Only OK/WARNING pass; any probe error refuses.

    A site that declares no UI Bridge preview is ``not_configured``, which
    REFUSES publication. There is no waiver (plan D7). In the publish path this
    runs inside :func:`d7_preview_page`, after the bridged page was navigated to
    this batch's preview."""
    return page_health_gate(
        f"site {site.id}",
        site.ui_bridge_url,
        probe,
        "site declares no UI Bridge preview; D7 page health is required — refused",
    )


def _shown_summary(summary: str) -> str:
    """A rejected page-health summary: a status word (``CRITICAL``) is worth
    printing; anything else is the bridge's free text and is omitted."""
    return f"'{summary}'" if re.fullmatch(r"[A-Za-z_]{1,32}", summary) else omitted(summary)


def page_health_gate(
    subject: str,
    ui_bridge_url: str | None,
    probe: PageHealthProbe = http_page_health_probe,
    missing: str = "no UI Bridge preview declared; D7 page health is required — refused",
) -> GateResult:
    """The target-neutral D7 page-health gate (a site, a launch bundle)."""
    if not ui_bridge_url:
        return GateResult("d7_page_health", subject, "not_configured", missing)
    try:
        body = probe(ui_bridge_url.rstrip("/") + "/control/page-health")
    except Exception as e:  # any probe failure is UNKNOWN; UNKNOWN does not publish
        return GateResult(
            "d7_page_health",
            subject,
            "fail",
            f"page-health probe failed: {type(e).__name__}: {omitted(e)}",
        )
    if not isinstance(body, dict):
        return GateResult("d7_page_health", subject, "fail", "page-health answered a non-object")
    data = body.get("data")
    summary = data.get("summary") if isinstance(data, dict) else None
    if body.get("success") is not True or not isinstance(summary, str):
        return GateResult(
            "d7_page_health", subject, "fail", f"unreadable page-health answer: {omitted(body)}"
        )
    if summary.upper() not in ACCEPTED_SUMMARIES:
        return GateResult(
            "d7_page_health",
            subject,
            "fail",
            f"page-health summary {_shown_summary(summary)} "
            f"(only {'/'.join(ACCEPTED_SUMMARIES)} publish)",
        )
    return GateResult("d7_page_health", subject, "pass", f"page-health summary {summary}")


# ---- the UI Bridge that D7 drives ---------------------------------------------


class UiBridge(Protocol):
    """The three UI Bridge control routes D7 uses (knowledge-base ``ui-bridge.md``):
    ``POST /control/page/navigate`` (drive the bridged page to a URL),
    ``GET /control/snapshot`` (``page.url`` = the current URL; media elements
    carry ``mediaMetadata`` with ``src``, ``naturalWidth`` and ``loadingState``)
    and ``POST /control/page-health``."""

    def navigate(self, url: str, *, hard: bool = True) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...

    def page_health(self) -> dict[str, Any]: ...


BridgeFactory = Callable[[str], UiBridge]


class BridgeError(Exception):
    """The bridge refused or could not deliver a command."""


class HttpUiBridge:
    """The UI Bridge control API over HTTP. ``base_url`` is the prefix the
    ``/control/*`` routes hang off for the bridge of the browser showing the
    preview; the preview page must run the React auto-registration provider
    with media discovery (an injected bridge registers no media)."""

    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30.0)

    def _body(self, resp: httpx.Response) -> dict[str, Any]:
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise BridgeError("bridge answered a non-object body")
        # A no-tab drive call is an HTTP 200 with success:false (ui-bridge.md):
        # branch on `success`, never on the status.
        if body.get("success") is not True:
            raise BridgeError(f"bridge refused: {body.get('error') or body}")
        return body

    def navigate(self, url: str, *, hard: bool = True) -> None:
        # ``hard: true`` forces a full document load. Without it, a page that
        # registered a navigateHandler (any SPA router) does a CLIENT-side
        # route change: the URL updates at once while the old document's DOM
        # stays, and D7 would judge a stale page.
        body = self._body(
            self.http.post(
                f"{self.base}/control/page/navigate",
                json={"url": url, "mode": "hard" if hard else "soft", "hard": hard},
            )
        )
        data = body.get("data")
        if isinstance(data, dict) and data.get("delivered") is False:
            raise BridgeError("navigate was not delivered to any tab")
        # navigate is fire-and-forget (DELIVERED, not EXECUTED); the caller
        # polls the snapshot until the page reports the target URL.

    def snapshot(self) -> dict[str, Any]:
        data = self._body(self.http.get(f"{self.base}/control/snapshot")).get("data")
        if not isinstance(data, dict):
            raise BridgeError("snapshot carried no data")
        return data

    def page_health(self) -> dict[str, Any]:
        return self._body(self.http.post(f"{self.base}/control/page-health"))


def preview_target(site: Site, batch_id: str, attempt: str) -> str:
    """The preview URL for one D7 attempt: the batch id AND a fresh attempt
    token ride in the query string, so the bridged page's own URL proves it
    loaded THIS attempt — a tab still showing an earlier attempt's document
    for the same batch never matches."""
    assert site.preview_url is not None
    return with_query(site.preview_url, {"studio_batch": batch_id, "studio_attempt": attempt})


def with_query(url: str, params: dict[str, str]) -> str:
    """``url`` with ``params`` appended to its REAL query (urlsplit/urlunsplit:
    an existing query is kept, never put inside a fragment)."""
    parts = urlsplit(url)
    encoded = urlencode(params)
    query = f"{parts.query}&{encoded}" if parts.query else encoded
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


_DEFAULT_PORTS = {"http": 80, "https": 443}


_UNPARSEABLE_PORT = -1


def normalise_url(url: str) -> tuple[str, str, int | None, str]:
    """(scheme, host, port, path) as a browser would report them: lowercase
    scheme and host, the default port dropped, dot segments resolved, an empty
    path as ``/`` (WHATWG URL serialisation, for the parts D7 compares).

    A URL ``urlparse`` cannot read (a non-numeric port, a broken IPv6 host)
    comes back with port ``-1``, which no real URL carries, so it matches
    nothing. Raising instead would carry the bridge's URL text out in the
    ``ValueError`` message, past every gate's allowlist."""
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return "", "", _UNPARSEABLE_PORT, ""
    scheme = p.scheme.lower()
    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    path = p.path or "/"
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    if path.endswith("/") and norm != "/":
        norm += "/"
    return scheme, (p.hostname or "").lower(), port, norm


def origin(url: str) -> tuple[str, str, int | None]:
    scheme, host, port, _ = normalise_url(url)
    return scheme, host, port


def same_page(
    actual: str | None,
    target: str,
    params: tuple[str, ...] = ("studio_batch", "studio_attempt"),
) -> bool:
    """Whether the bridged page's URL is ``target``: same origin and path
    (browser-normalised) and the same values for ``params`` (by default
    ``studio_batch`` and ``studio_attempt``; a launch uses ``studio_launch``)."""
    if not actual:
        return False
    if normalise_url(actual) != normalise_url(target):
        return False
    a, t = urlparse(actual), urlparse(target)
    if a.fragment != t.fragment:
        return False
    got, want = parse_qs(a.query), parse_qs(t.query)
    return all(got.get(k) == want.get(k) for k in params)


def _shown_url(url: str | None) -> str:
    """A bridge-reported URL as a gate detail may print it: scheme, host, port
    and path. Its query, fragment and userinfo are page-controlled free text (a
    login redirect's ``?code=``, ``user:pw@``) and are left out; a URL that
    does not parse is omitted whole."""
    if url is None:
        return "None"
    scheme, host, port, path = normalise_url(url)
    if port == _UNPARSEABLE_PORT:
        return omitted(url)
    netloc = host if port is None else f"{host}:{port}"
    rest = " (query omitted)" if urlsplit(url).query else ""
    return f"'{scheme}://{netloc}{path}'{rest}"


def _current_url(snapshot: dict[str, Any]) -> str | None:
    page = snapshot.get("page")
    url = page.get("url") if isinstance(page, dict) else None
    return url if isinstance(url, str) else None


def slot_media_ids(slot: str) -> tuple[str, str]:
    """The ``data-media-id`` a site template gives a studio slot's ``<picture>``
    and its ``<img>``: ``studio-picture-<slot>`` and ``studio-img-<slot>``. The
    kind comes BEFORE the slot name, so no two slots' ids can collide (a
    ``<slot>-img`` suffix would make slot ``hero``'s img and slot ``hero-img``'s
    picture the same id). UI Bridge's ``generateMediaId`` honours an explicit
    ``data-media-id`` first; without it, ids come from the alt text (the same
    site-wide label on every slot) or sibling index and COLLIDE, and the
    registry silently drops every duplicate — so D7 identifies slots by these
    ids, never by guessing from file names."""
    return f"studio-picture-{slot}", f"studio-img-{slot}"


def _media(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Media elements by id."""
    out: dict[str, dict[str, Any]] = {}
    for element in snapshot.get("elements") or []:
        if not isinstance(element, dict):
            continue
        meta = element.get("mediaMetadata")
        if isinstance(meta, dict) and isinstance(element.get("id"), str):
            out[element["id"]] = meta
    return out


NO_MEDIA = (
    "the preview's UI Bridge reports no media elements at all; D7 needs the React "
    "auto-registration provider with media discovery (an injected/seeded bridge "
    "registers interactive elements only)"
)


@dataclass(frozen=True)
class PreviewSpec:
    """What the target-neutral D7 core needs to know about a preview: a site's
    (built by :func:`site_preview`) or a launch bundle's.

    Groups are slots (a site) or bundle images (a launch); each group's
    elements carry ``data-media-id`` ``studio-picture-<group>`` /
    ``studio-img-<group>``. ``files`` keys are file names (a site) or
    preview-relative paths (a launch bundle); either way the preview serves
    each one at ``urljoin(preview_manifest_url, key)``."""

    subject: str
    preview_url: str
    preview_manifest_url: str | None
    # Groups not shown on the page (og:image-like): fetch-checked instead.
    unrendered: frozenset[str] = frozenset()
    # The query parameters that must match between the target and the page.
    params: tuple[str, ...] = ("studio_batch", "studio_attempt")


def site_preview(site: Site) -> PreviewSpec:
    assert site.preview_url is not None
    return PreviewSpec(
        subject=f"site {site.id}",
        preview_url=site.preview_url,
        preview_manifest_url=site.preview_manifest_url,
        unrendered=frozenset(name for name, slot in site.slots.items() if not slot.rendered),
    )


def expected_file_url(site: Site, name: str) -> str:
    """Where the preview serves a slot file: beside the manifest, since both
    live in ``preview_dir``."""
    return expected_url(site_preview(site), name)


def expected_url(spec: PreviewSpec, name: str) -> str:
    """The target-neutral :func:`expected_file_url`."""
    return urljoin(spec.preview_manifest_url or spec.preview_url, name)


def _file_at(spec: PreviewSpec, url: str, files: dict[str, str]) -> str | None:
    """The file ``url`` (already absolute) points at, matching scheme, host,
    port and the FULL path against where the preview serves it — never just
    the basename — or None."""
    for name in files:
        if normalise_url(url) == normalise_url(expected_url(spec, name)):
            return name
    return None


def _names_a_file(url: str, files: dict[str, str]) -> bool:
    """Whether a URL's file name is one of ``files``' (by basename) — a
    candidate that must then match its full preview URL exactly."""
    leaf = PurePosixPath(urlparse(url).path).name
    return any(PurePosixPath(name).name == leaf for name in files)


def _slot_elements(
    spec: PreviewSpec,
    page_url: str,
    media: dict[str, dict[str, Any]],
    slot: str,
    files: dict[str, str],
) -> tuple[list[tuple[str, dict[str, Any], str | None, list[str] | None]], list[str]]:
    """The slot's elements, found by ``data-media-id``: (id, metadata, file
    name or None when the source is not exactly one of the slot's preview file
    URLs, ?sha= values). Also returns the problems seen while matching."""
    picture_id, img_id = slot_media_ids(slot)
    found: list[tuple[str, dict[str, Any], str | None, list[str] | None]] = []
    problems: list[str] = []
    for mid in (picture_id, img_id):
        meta = media.get(mid)
        if meta is None:
            continue
        src_raw = meta.get("src")
        if not isinstance(src_raw, str) or not src_raw:
            problems.append(
                f"slot '{slot}' element '{mid}' has no src — the page rendered no image for "
                "this slot (e.g. the template's manifest lookup missed)"
            )
            found.append((mid, meta, None, None))
            continue
        src = urljoin(page_url, src_raw)
        name = _file_at(spec, src, files)
        if name is None:
            expected = ", ".join(expected_url(spec, n) for n in sorted(files))
            problems.append(
                f"slot '{slot}' element '{mid}' shows {src}, which is not one of the slot's "
                f"preview files ({expected})"
            )
            found.append((mid, meta, None, None))
            continue
        found.append((mid, meta, name, parse_qs(urlparse(src).query).get("sha")))
    return found, problems


def _srcset_urls(meta: dict[str, Any]) -> list[str]:
    """Candidate URLs from an element's own ``srcset`` and, when the bridge
    reports them, its ``<picture>`` ``sources[].srcset``."""
    sets: list[str] = []
    if isinstance(meta.get("srcset"), str):
        sets.append(meta["srcset"])
    for source in meta.get("sources") or []:
        if isinstance(source, dict) and isinstance(source.get("srcset"), str):
            sets.append(source["srcset"])
    urls: list[str] = []
    for srcset in sets:
        for candidate in srcset.split(","):
            parts = candidate.strip().split()
            if parts:
                urls.append(parts[0])
    return urls


def images_settled(
    site: Site, snapshot: dict[str, Any], slot_files: dict[str, dict[str, str]]
) -> bool:
    """Whether every rendered changed slot has at least one element on the
    page and none of them is still ``pending`` — i.e. waiting longer cannot
    change the verdict."""
    if not site.preview_url:
        return True
    return preview_images_settled(site_preview(site), snapshot, slot_files)


def preview_images_settled(
    spec: PreviewSpec, snapshot: dict[str, Any], groups: dict[str, dict[str, str]]
) -> bool:
    """The target-neutral :func:`images_settled`."""
    page_url = _current_url(snapshot) or spec.preview_url
    media = _media(snapshot)
    for slot, files in groups.items():
        if slot in spec.unrendered:
            continue
        found, _ = _slot_elements(spec, page_url, media, slot, files)
        # Settled only once some element actually shows one of the slot's
        # files and none is still loading; a src-less element never settles.
        if not any(name is not None for _, _, name, _ in found):
            return False
        if any(m.get("loadingState") == "pending" for _, m, _, _ in found):
            return False
    return True


def _hash_check(slot: str, url: str, sha: str, fetch: Callable[[str], bytes]) -> str | None:
    try:
        got = hashlib.sha256(fetch(url)).hexdigest()
    except Exception as e:  # unreachable is UNKNOWN; UNKNOWN does not publish
        return f"slot '{slot}' {url} could not be fetched ({type(e).__name__})"
    if got != sha:
        return f"slot '{slot}' {url} is not this batch's bytes"
    return None


def _fetch_check(
    spec: PreviewSpec, slot: str, name: str, sha: str, fetch: Callable[[str], bytes]
) -> str | None:
    return _hash_check(slot, expected_url(spec, name), sha, fetch)


def d7_images(
    site: Site,
    snapshot: dict[str, Any],
    slot_files: dict[str, dict[str, str]],
    fetch: Callable[[str], bytes],
) -> GateResult:
    """Every slot this batch changes is really on the preview, as THIS batch's bytes.

    ``slot_files`` maps slot -> {file name: sha256}. A browser renders a
    multi-format slot through ``<picture>``, fetching ONE file, and the bridge
    reports only the image's ``currentSrc``. So, for a rendered slot:

    * at least one image element must show one of the slot's files from the
      preview's origin, carrying ``?sha=<that file's sha256>`` (the manifest's
      cache-buster), loaded (``naturalWidth > 0``, ``loadingState ==
      'loaded'``); ``lazy`` is refused (studio slots must be ``loading="eager"``);
    * every element showing a slot file must carry that file's ``?sha=``, and
      every ``srcset`` / ``<picture>`` source URL naming a slot file must too;
    * every format NOT shown by a loaded element is fetched from the preview
      server and its sha256 compared.

    A slot declared ``rendered: false`` (meta only, e.g. ``og:image``) is
    verified by fetching every file."""
    if not site.preview_url:
        return GateResult(
            "d7_images", f"site {site.id}", "not_configured", "site declares no preview_url"
        )
    return images_gate(site_preview(site), snapshot, slot_files, fetch)


def images_gate(
    spec: PreviewSpec,
    snapshot: dict[str, Any],
    slot_files: dict[str, dict[str, str]],
    fetch: Callable[[str], bytes],
) -> GateResult:
    """The target-neutral :func:`d7_images` (same rules, any preview)."""
    subject = spec.subject
    page_url = _current_url(snapshot) or spec.preview_url
    media = _media(snapshot)
    problems: list[str] = []
    if any(slot not in spec.unrendered for slot in slot_files) and not media:
        return GateResult("d7_images", subject, "fail", NO_MEDIA)
    for slot, files in sorted(slot_files.items()):
        to_fetch = dict(files)
        if slot not in spec.unrendered:
            found, matched_problems = _slot_elements(spec, page_url, media, slot, files)
            problems += matched_problems
            if not found:
                picture_id, img_id = slot_media_ids(slot)
                problems.append(
                    f"slot '{slot}' is not on the preview page: no element with "
                    f'data-media-id="{picture_id}" or "{img_id}"'
                )
                continue
            shown_ok = False
            for mid, m, name, tag in found:
                if name is None:
                    continue
                state = m.get("loadingState")
                if tag != [files[name]]:
                    problems.append(
                        f"slot '{slot}' element '{mid}' shows {m['src']}, not this batch's "
                        f"{name} (?sha=)"
                    )
                elif state == "lazy":
                    problems.append(
                        f"slot '{slot}' element '{mid}' is lazy-loaded; studio slots must be "
                        'rendered with loading="eager" so D7 can see them load'
                    )
                elif state == "pending":
                    problems.append(f"slot '{slot}' element '{mid}' did not finish loading")
                elif (
                    not (isinstance(m.get("naturalWidth"), int | float) and m["naturalWidth"] > 0)
                    or state != "loaded"
                ):
                    problems.append(
                        f"slot '{slot}' element '{mid}' ({m['src']}) did not load "
                        f"(naturalWidth={m.get('naturalWidth')}, state={state})"
                    )
                else:
                    # Hash the bytes at the exact URL the element shows.
                    shown_url = urljoin(page_url, m["src"])
                    problem = _hash_check(slot, shown_url, files[name], fetch)
                    if problem:
                        problems.append(problem)
                    else:
                        shown_ok = True
                        to_fetch.pop(name, None)
                for url in _srcset_urls(m):
                    resolved = urljoin(page_url, url)
                    if not _names_a_file(resolved, files):
                        continue
                    sname = _file_at(spec, resolved, files)
                    if sname is None:
                        problems.append(
                            f"slot '{slot}' source {resolved} is not one of the slot's preview files"
                        )
                    elif parse_qs(urlparse(resolved).query).get("sha") != [files[sname]]:
                        problems.append(
                            f"slot '{slot}' source {url} is not this batch's {sname} (?sha=)"
                        )
            if not shown_ok:
                problems.append(
                    f"slot '{slot}' shows none of its files ({', '.join(sorted(files))}) "
                    "loaded with this batch's ?sha="
                )
                continue
        for name, sha in sorted(to_fetch.items()):
            problem = _fetch_check(spec, slot, name, sha, fetch)
            if problem:
                problems.append(problem)
    if problems:
        return GateResult("d7_images", subject, "fail", "; ".join(problems))
    return GateResult(
        "d7_images", subject, "pass", f"{len(slot_files)} slot(s) verified on the preview"
    )


def d7_preview_page(
    site: Site,
    batch_id: str,
    slot_files: dict[str, dict[str, str]],
    bridge_factory: BridgeFactory,
    fetch: Callable[[str], bytes],
    *,
    deadline_s: float = 30.0,
    poll_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[GateResult]:
    """D7 bound to THIS batch's preview: navigate the bridged page to the
    batch's preview URL, poll its snapshot until the page reports that URL
    (browser-normalised; a bridge error while the tab re-registers is retried
    until the deadline), require its origin to be the preview's, run page
    health, confirm the page did not move, and verify every changed slot."""
    if not (site.ui_bridge_url and site.preview_url):
        return [d7_page_health(site)]
    return preview_page_gates(
        site_preview(site),
        site.ui_bridge_url,
        preview_target(site, batch_id, uuid.uuid4().hex),
        slot_files,
        bridge_factory,
        fetch,
        what="this batch's preview",
        deadline_s=deadline_s,
        poll_s=poll_s,
        sleep=sleep,
        monotonic=monotonic,
    )


def preview_page_gates(
    spec: PreviewSpec,
    ui_bridge_url: str,
    target: str,
    slot_files: dict[str, dict[str, str]],
    bridge_factory: BridgeFactory,
    fetch: Callable[[str], bytes],
    *,
    what: str = "the preview",
    deadline_s: float = 30.0,
    poll_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[GateResult]:
    """The target-neutral :func:`d7_preview_page`: navigate the bridge to
    ``target`` (which carries a fresh attempt token), poll until the page
    reports it (browser-normalised, every ``spec.params`` equal, the preview's
    origin), page health, then poll until the images settle (the page must not
    move) and run :func:`images_gate`."""
    subject = spec.subject
    stop = monotonic() + deadline_s
    bridge: UiBridge | None = None
    navigated = False
    seen: str | None = None
    last_error = ""
    before: dict[str, Any] | None = None
    while True:
        try:
            if bridge is None:
                bridge = bridge_factory(ui_bridge_url)
            if not navigated:
                bridge.navigate(target, hard=True)
                navigated = True
            snap = bridge.snapshot()
            seen = _current_url(snap)
            if same_page(seen, target, spec.params):
                before = snap
                break
        except Exception as e:  # e.g. the tab is re-registering after navigation
            last_error = f"{type(e).__name__}: {omitted(e)}"
        if monotonic() >= stop:
            break
        sleep(poll_s)
    if before is None or bridge is None:
        why = (
            f"bridged page is at {_shown_url(seen)}, not {what} {target!r}"
            if seen is not None or not last_error
            else f"bridge unavailable: {last_error}"
        )
        if seen is not None and origin(seen) != origin(target):
            why += f" (origin {origin(seen)} is not the preview origin {origin(target)})"
        return [GateResult("d7_navigate", subject, "fail", f"{why} after {deadline_s:.0f}s")]
    results = [GateResult("d7_navigate", subject, "pass", f"bridged page loaded {target}")]
    results.append(page_health_gate(subject, ui_bridge_url, lambda _url: bridge.page_health()))
    # Poll until every changed slot's image has settled (loaded or failed),
    # within the remaining deadline; the page must not move meanwhile.
    after: dict[str, Any] | None = None
    while True:
        try:
            snap = bridge.snapshot()
        except Exception as e:
            last_error = f"{type(e).__name__}: {omitted(e)}"
        else:
            if not same_page(_current_url(snap), target, spec.params):
                results.append(
                    GateResult(
                        "d7_navigate",
                        subject,
                        "fail",
                        f"page moved to {_shown_url(_current_url(snap))} during the D7 check",
                    )
                )
                return results
            after = snap
            if preview_images_settled(spec, snap, slot_files):
                break
        if monotonic() >= stop:
            break
        sleep(poll_s)
    if after is None:
        results.append(
            GateResult("d7_navigate", subject, "fail", f"bridge unavailable: {last_error}")
        )
        return results
    results.append(images_gate(spec, after, slot_files, fetch))
    return results


ManifestFetch = Callable[[str], bytes]


def http_manifest_fetch(url: str) -> bytes:
    resp = httpx.get(url, timeout=30.0)
    resp.raise_for_status()
    return resp.content


def d7_preview_manifest(
    site: Site, expected_sha256: str, fetch: ManifestFetch = http_manifest_fetch
) -> GateResult:
    """Confirm the preview the page-health probe will look at is actually
    serving THIS batch: the manifest it serves must hash to the one just written."""
    return preview_manifest_gate(
        f"site {site.id}",
        site.preview_manifest_url,
        expected_sha256,
        fetch,
        "this batch's",
        "site declares no preview_manifest_url",
    )


def preview_manifest_gate(
    subject: str,
    preview_manifest_url: str | None,
    expected_sha256: str,
    fetch: ManifestFetch = http_manifest_fetch,
    what: str = "this batch's",
    missing: str = "no preview_manifest_url declared; D7 is required — refused",
) -> GateResult:
    """The target-neutral preview-identity gate: the manifest the preview
    serves must hash to the one just staged."""
    if not preview_manifest_url:
        return GateResult("d7_preview", subject, "not_configured", missing)
    try:
        served = fetch(preview_manifest_url)
    except Exception as e:  # an unreachable preview is a refusal
        return GateResult(
            "d7_preview",
            subject,
            "fail",
            f"preview manifest fetch failed: {type(e).__name__}: {omitted(e)}",
        )
    digest = hashlib.sha256(served).hexdigest()
    if digest != expected_sha256:
        return GateResult(
            "d7_preview",
            subject,
            "fail",
            f"preview serves manifest {digest[:12]}, not {what} {expected_sha256[:12]}",
        )
    return GateResult("d7_preview", subject, "pass", f"preview serves manifest {digest[:12]}")
