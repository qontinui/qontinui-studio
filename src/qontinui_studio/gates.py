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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

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

    def navigate(self, url: str) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...

    def page_health(self) -> dict[str, Any]: ...


BridgeFactory = Callable[[str], UiBridge]


class BridgeError(Exception):
    """The bridge refused or could not deliver a command."""


class HttpUiBridge:
    """The UI Bridge control API over HTTP. ``base_url`` is the prefix the
    ``/control/*`` routes hang off, e.g. ``http://127.0.0.1:3000/__ui-bridge``
    for the injection proxy in front of a preview server."""

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

    def navigate(self, url: str) -> None:
        body = self._body(
            self.http.post(f"{self.base}/control/page/navigate", json={"url": url, "mode": "hard"})
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


def preview_target(site: Site, batch_id: str) -> str:
    """The preview URL for one batch: the batch id rides in the query string,
    so the bridged page's own URL proves which batch it loaded."""
    assert site.preview_url is not None
    sep = "&" if "?" in site.preview_url else "?"
    return f"{site.preview_url}{sep}{urlencode({'studio_batch': batch_id})}"


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


def same_page(actual: str | None, target: str, batch_id: str) -> bool:
    """Whether the bridged page's URL is this batch's preview: same origin and
    path (browser-normalised) and ``studio_batch`` = this batch."""
    if not actual:
        return False
    if normalise_url(actual) != normalise_url(target):
        return False
    return parse_qs(urlparse(actual).query).get("studio_batch") == [batch_id]


def _current_url(snapshot: dict[str, Any]) -> str | None:
    page = snapshot.get("page")
    url = page.get("url") if isinstance(page, dict) else None
    return url if isinstance(url, str) else None


def d7_images(
    site: Site,
    snapshot: dict[str, Any],
    slot_files: dict[str, dict[str, str]],
    fetch: Callable[[str], bytes],
) -> GateResult:
    """Every slot this batch changes is really on the preview, as THIS batch's bytes.

    ``slot_files`` maps slot -> {file name: sha256}. For a rendered slot, EVERY
    file must appear as an image element whose source resolves under the
    preview's origin, carries ``?sha=<that file's sha256>`` (the cache-buster
    the manifest gives the template), and loaded (``naturalWidth > 0``,
    ``loadingState == 'loaded'``). A slot declared ``rendered: false`` (meta
    only, e.g. an ``og:image``) is verified by fetching each file from the
    preview server and comparing its sha256."""
    subject = f"site {site.id}"
    if not site.preview_url:
        return GateResult("d7_images", subject, "not_configured", "site declares no preview_url")
    page_url = _current_url(snapshot) or site.preview_url
    preview_origin = origin(site.preview_url)
    media: list[dict[str, Any]] = []
    for element in snapshot.get("elements") or []:
        meta = element.get("mediaMetadata") if isinstance(element, dict) else None
        if isinstance(meta, dict) and isinstance(meta.get("src"), str):
            media.append(meta)
    problems: list[str] = []
    for slot, files in sorted(slot_files.items()):
        if not site.slots[slot].rendered:
            for name, sha in sorted(files.items()):
                url = urljoin(site.preview_url, name)
                try:
                    got = hashlib.sha256(fetch(url)).hexdigest()
                except Exception as e:  # unreachable is UNKNOWN; UNKNOWN does not publish
                    problems.append(
                        f"slot '{slot}' {url} could not be fetched ({type(e).__name__})"
                    )
                    continue
                if got != sha:
                    problems.append(f"slot '{slot}' {url} is not this batch's bytes")
            continue
        for name, sha in sorted(files.items()):
            shown = []
            for m in media:
                src = urljoin(page_url, m["src"])
                parsed = urlparse(src)
                if origin(src) != preview_origin or PurePosixPath(parsed.path).name != name:
                    continue
                shown.append((m, parse_qs(parsed.query).get("sha")))
            if not shown:
                problems.append(
                    f"slot '{slot}' file {name} is not rendered from the preview "
                    f"({site.preview_url}) — maybe served from another host, or not at all"
                )
                continue
            for m, tag in shown:
                if tag != [sha]:
                    problems.append(f"slot '{slot}' {m['src']} is not this batch's {name} (?sha=)")
                elif not (
                    isinstance(m.get("naturalWidth"), int | float) and m["naturalWidth"] > 0
                ) or (m.get("loadingState") != "loaded"):
                    problems.append(
                        f"slot '{slot}' image {m['src']} did not load "
                        f"(naturalWidth={m.get('naturalWidth')}, state={m.get('loadingState')})"
                    )
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
    target = preview_target(site, batch_id)
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
                bridge.navigate(target)
                navigated = True
            snap = bridge.snapshot()
            seen = _current_url(snap)
            if same_page(seen, target, batch_id):
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
    try:
        after = bridge.snapshot()
    except Exception as e:
        results.append(GateResult("d7_navigate", subject, "fail", f"{type(e).__name__}: {e}"))
        return results
    if not same_page(_current_url(after), target, batch_id):
        results.append(
            GateResult(
                "d7_navigate",
                subject,
                "fail",
                f"page moved to {_current_url(after)!r} during the page-health check",
            )
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
