"""D5 / D6 / D7 publication gates as code.

Each gate is a pure function over the library records and the site config,
returning a :class:`GateResult`. A batch publishes only when no gate fails
(D7: automated gates decide publication; the operator is notified, not asked).

D3 (the build-record export allowlist) is a gate on the FACTORY's export
(Phase 1, qontinui-coord) and has no subject in this repo.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from qontinui_studio.library import AssetRecord, VariantRecord
from qontinui_studio.postprocess import sha256_file
from qontinui_studio.sites import Site, Slot

# ``not_configured`` is a FAILURE (D7: a gate that did not run did not pass);
# ``waived`` passes but is printed in every operator notice.
Outcome = Literal["pass", "fail", "not_configured", "waived"]


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


def http_page_health_probe(url: str) -> dict[str, object]:
    """POST the UI Bridge page-health endpoint (see the knowledge-base doc
    ``page-health-diagnostic.md``) and return its JSON body."""
    resp = httpx.post(url, timeout=30.0)
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("page-health answered a non-object body")
    return body


def d7_page_health(site: Site, probe: PageHealthProbe = http_page_health_probe) -> GateResult:
    """A UI Bridge page-health pass over the site's running preview.

    A site that declares no ``page_health_url`` is ``not_configured``, which
    REFUSES publication, unless it is a demo site with ``page_health: waived`` —
    then the outcome is ``waived`` and every operator notice prints it."""
    subject = f"site {site.id}"
    if not site.page_health_url:
        if site.page_health == "waived":
            return GateResult(
                "d7_page_health",
                subject,
                "waived",
                f"page health WAIVED for demo site '{site.id}' (no running preview)",
            )
        return GateResult(
            "d7_page_health",
            subject,
            "not_configured",
            "site declares no page_health_url and does not waive page health — refused",
        )
    try:
        body = probe(site.page_health_url)
    except (httpx.HTTPError, ValueError) as e:
        # Unreachable is UNKNOWN, and UNKNOWN does not publish.
        return GateResult("d7_page_health", subject, "fail", f"page-health probe failed: {e}")
    data = body.get("data")
    summary = data.get("summary") if isinstance(data, dict) else None
    if body.get("success") is not True or not isinstance(summary, str):
        return GateResult(
            "d7_page_health", subject, "fail", f"unreadable page-health answer: {body}"
        )
    if summary.upper() == "CRITICAL":
        return GateResult("d7_page_health", subject, "fail", "page-health summary CRITICAL")
    return GateResult("d7_page_health", subject, "pass", f"page-health summary {summary}")
