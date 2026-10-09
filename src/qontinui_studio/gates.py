"""D5 / D6 / D7 publication gates as code.

Each gate is a pure function over the library records and the site config,
returning a :class:`GateResult`. A batch publishes only when no gate fails
(D7: automated gates decide publication; the operator is notified, not asked).

D3 (the build-record export allowlist) is a gate on the FACTORY's export
(Phase 1, qontinui-coord) and has no subject in this repo.
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
    return GateResult("d5_provenance", subject, "pass", "provenance complete; digests match")


def d5_licence(asset: AssetRecord, variant: VariantRecord, site: Site) -> GateResult:
    """Refuse an unreviewed licence anywhere, and a non-commercial one on a commercial site."""
    subject = _subject(asset, variant)
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
    if site.commercial and asset.licence_commercial_use != "yes":
        return GateResult(
            "d5_licence",
            subject,
            "fail",
            f"{asset.provider}: commercial_use is '{asset.licence_commercial_use}' "
            f"but site '{site.id}' is commercial",
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
    subject = f"site {site.id}"
    if not site.ui_bridge_url:
        return GateResult(
            "d7_page_health",
            subject,
            "not_configured",
            "site declares no UI Bridge preview; D7 page health is required — refused",
        )
    try:
        body = probe(site.ui_bridge_url.rstrip("/") + "/control/page-health")
    except Exception as e:  # any probe failure is UNKNOWN; UNKNOWN does not publish
        return GateResult(
            "d7_page_health", subject, "fail", f"page-health probe failed: {type(e).__name__}: {e}"
        )
    if not isinstance(body, dict):
        return GateResult("d7_page_health", subject, "fail", "page-health answered a non-object")
    data = body.get("data")
    summary = data.get("summary") if isinstance(data, dict) else None
    if body.get("success") is not True or not isinstance(summary, str):
        return GateResult(
            "d7_page_health", subject, "fail", f"unreadable page-health answer: {body}"
        )
    if summary.upper() not in ACCEPTED_SUMMARIES:
        return GateResult(
            "d7_page_health",
            subject,
            "fail",
            f"page-health summary '{summary}' (only {'/'.join(ACCEPTED_SUMMARIES)} publish)",
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
    parts = urlsplit(site.preview_url)
    params = urlencode({"studio_batch": batch_id, "studio_attempt": attempt})
    query = f"{parts.query}&{params}" if parts.query else params
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalise_url(url: str) -> tuple[str, str, int | None, str]:
    """(scheme, host, port, path) as a browser would report them: lowercase
    scheme and host, the default port dropped, dot segments resolved, an empty
    path as ``/`` (WHATWG URL serialisation, for the parts D7 compares)."""
    p = urlparse(url)
    scheme = p.scheme.lower()
    port = p.port
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


def same_page(actual: str | None, target: str) -> bool:
    """Whether the bridged page's URL is ``target``: same origin and path
    (browser-normalised) and the same ``studio_batch`` and ``studio_attempt``."""
    if not actual:
        return False
    if normalise_url(actual) != normalise_url(target):
        return False
    a, t = urlparse(actual), urlparse(target)
    if a.fragment != t.fragment:
        return False
    got, want = parse_qs(a.query), parse_qs(t.query)
    return all(got.get(k) == want.get(k) for k in ("studio_batch", "studio_attempt"))


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


def expected_file_url(site: Site, name: str) -> str:
    """Where the preview serves a slot file: beside the manifest, since both
    live in ``preview_dir``."""
    base = site.preview_manifest_url or site.preview_url
    assert base is not None
    return urljoin(base, name)


def _file_at(site: Site, url: str, files: dict[str, str]) -> str | None:
    """The slot file ``url`` (already absolute) points at, matching scheme,
    host, port and the FULL path against where the preview serves it — never
    just the basename — or None."""
    for name in files:
        if normalise_url(url) == normalise_url(expected_file_url(site, name)):
            return name
    return None


def _slot_elements(
    site: Site, page_url: str, media: dict[str, dict[str, Any]], slot: str, files: dict[str, str]
) -> tuple[list[tuple[str, dict[str, Any], str | None, list[str] | None]], list[str]]:
    """The slot's elements, found by ``data-media-id``: (id, metadata, file
    name or None when the source is not exactly one of the slot's preview file
    URLs, ?sha= values). Also returns the problems seen while matching."""
    assert site.preview_url is not None
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
        name = _file_at(site, src, files)
        if name is None:
            expected = ", ".join(expected_file_url(site, n) for n in sorted(files))
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
    page_url = _current_url(snapshot) or site.preview_url
    media = _media(snapshot)
    for slot, files in slot_files.items():
        if not site.slots[slot].rendered:
            continue
        found, _ = _slot_elements(site, page_url, media, slot, files)
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
    site: Site, slot: str, name: str, sha: str, fetch: Callable[[str], bytes]
) -> str | None:
    return _hash_check(slot, expected_file_url(site, name), sha, fetch)


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
    subject = f"site {site.id}"
    if not site.preview_url:
        return GateResult("d7_images", subject, "not_configured", "site declares no preview_url")
    page_url = _current_url(snapshot) or site.preview_url
    media = _media(snapshot)
    problems: list[str] = []
    if any(site.slots[slot].rendered for slot in slot_files) and not media:
        return GateResult("d7_images", subject, "fail", NO_MEDIA)
    for slot, files in sorted(slot_files.items()):
        to_fetch = dict(files)
        if site.slots[slot].rendered:
            found, matched_problems = _slot_elements(site, page_url, media, slot, files)
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
                    if PurePosixPath(urlparse(resolved).path).name not in files:
                        continue
                    sname = _file_at(site, resolved, files)
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
            problem = _fetch_check(site, slot, name, sha, fetch)
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
    subject = f"site {site.id}"
    if not (site.ui_bridge_url and site.preview_url):
        return [d7_page_health(site)]
    target = preview_target(site, batch_id, uuid.uuid4().hex)
    stop = monotonic() + deadline_s
    bridge: UiBridge | None = None
    navigated = False
    seen: str | None = None
    last_error = ""
    before: dict[str, Any] | None = None
    while True:
        try:
            if bridge is None:
                bridge = bridge_factory(site.ui_bridge_url)
            if not navigated:
                bridge.navigate(target, hard=True)
                navigated = True
            snap = bridge.snapshot()
            seen = _current_url(snap)
            if same_page(seen, target):
                before = snap
                break
        except Exception as e:  # e.g. the tab is re-registering after navigation
            last_error = f"{type(e).__name__}: {e}"
        if monotonic() >= stop:
            break
        sleep(poll_s)
    if before is None or bridge is None:
        why = (
            f"bridged page is at {seen!r}, not this batch's preview {target!r}"
            if seen is not None or not last_error
            else f"bridge unavailable: {last_error}"
        )
        if seen is not None and origin(seen) != origin(target):
            why += f" (origin {origin(seen)} is not the preview origin {origin(target)})"
        return [GateResult("d7_navigate", subject, "fail", f"{why} after {deadline_s:.0f}s")]
    results = [GateResult("d7_navigate", subject, "pass", f"bridged page loaded {seen}")]
    results.append(d7_page_health(site, lambda _url: bridge.page_health()))
    # Poll until every changed slot's image has settled (loaded or failed),
    # within the remaining deadline; the page must not move meanwhile.
    after: dict[str, Any] | None = None
    while True:
        try:
            snap = bridge.snapshot()
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        else:
            if not same_page(_current_url(snap), target):
                results.append(
                    GateResult(
                        "d7_navigate",
                        subject,
                        "fail",
                        f"page moved to {_current_url(snap)!r} during the D7 check",
                    )
                )
                return results
            after = snap
            if images_settled(site, snap, slot_files):
                break
        if monotonic() >= stop:
            break
        sleep(poll_s)
    if after is None:
        results.append(
            GateResult("d7_navigate", subject, "fail", f"bridge unavailable: {last_error}")
        )
        return results
    results.append(d7_images(site, after, slot_files, fetch))
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
    subject = f"site {site.id}"
    if not site.preview_manifest_url:
        return GateResult(
            "d7_preview", subject, "not_configured", "site declares no preview_manifest_url"
        )
    try:
        served = fetch(site.preview_manifest_url)
    except Exception as e:  # an unreachable preview is a refusal
        return GateResult(
            "d7_preview", subject, "fail", f"preview manifest fetch failed: {type(e).__name__}: {e}"
        )
    digest = hashlib.sha256(served).hexdigest()
    if digest != expected_sha256:
        return GateResult(
            "d7_preview",
            subject,
            "fail",
            f"preview serves manifest {digest[:12]}, not this batch's {expected_sha256[:12]}",
        )
    return GateResult("d7_preview", subject, "pass", f"preview serves manifest {digest[:12]}")
