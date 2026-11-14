"""qontinui-web's content-addressed image store (plan 2026-10-13 Phase 13),
as ``studio compile`` writes to it (Phase 19 step 3).

* ``POST /api/v1/image-assets`` (multipart ``file`` + ``kind`` +
  ``provenance``): the server hashes the bytes and keys the object
  ``image-assets/{tenant}/sha256/{h[:2]}/{h}{ext}``; the same bytes again answer
  200 ``created: false``. Every candidate goes up as ``candidate``, the picked
  one's derivatives as ``derivative``.
* ``PUT /api/v1/image-assets/slot-candidates``: each candidate is proposed for
  ``(repo, slot_id)`` with its recipe hash and judge record; the published pick
  is moved to ``chosen``.

Auth is the fleet's device-JWT chain (:mod:`qontinui_studio.device_auth`); the
tenant is the credential's, never sent. Every call is keyed by ONE repository,
the compile target's, so before the first write the store proves which
credential acts for the tenant that OWNS that repository (``prove_owner``,
served ``coordination`` ``repo-writes-go-to-the-owning-tenant``) and then uses
that credential alone; a repository whose owner is refuted or UNKNOWN gets no
write at all (``refused``). The runner's minted token is offered only when both
the web API and coord are production, and no token goes to a door that is
neither https nor loopback (``$STUDIO_WEB_URL``).

Every call answers a :class:`StoreOutcome`. Anything but an answer that names
the bytes' own hash is ``unknown`` — a store that did not confirm the upload
never reads as stored — and a 4xx refusal is ``refused`` with the store's code.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from qontinui_studio.device_auth import (
    Credential,
    DeviceCredentials,
    OwnerProof,
    https_or_loopback,
    prove_owner,
)
from qontinui_studio.gateway import runner_url
from qontinui_studio.imagegen_policy import PRODUCTION_COORD, coord_base_url
from qontinui_studio.postprocess import sha256_file

WEB_URL_ENV = "STUDIO_WEB_URL"
PRODUCTION_WEB = "https://api.qontinui.io"
UPLOAD_PATH = "/api/v1/image-assets"
CANDIDATES_PATH = "/api/v1/image-assets/slot-candidates"
# The store's own caps (qontinui-web app/schemas/image_assets.py).
MAX_PROVENANCE_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
TIMEOUT_S = 60.0
# Kept from a sidecar too large for the store's provenance field.
_ESSENTIAL = (
    "schema",
    "ai_generated",
    "digital_source_type",
    "file",
    "provider",
    "model",
    "terms",
    "seed",
    "recipe",
    "created_at",
    "job_id",
    "batch_id",
    "brief_id",
)

AssetKind = Literal["candidate", "derivative", "capture", "exemplar"]
CandidateStatus = Literal["candidate", "chosen", "rejected", "shipped"]
OutcomeStatus = Literal["stored", "existing", "proposed", "refused", "unknown"]


@dataclass(frozen=True)
class StoreOutcome:
    status: OutcomeStatus
    detail: str

    @property
    def confirmed(self) -> bool:
        return self.status in ("stored", "existing", "proposed")


class ImageStore(Protocol):
    def upload(
        self, path: Path, kind: AssetKind, provenance: dict[str, Any] | None
    ) -> StoreOutcome: ...

    def propose(
        self,
        *,
        repo: str,
        slot_id: str,
        asset_sha256: str,
        status: CandidateStatus,
        recipe_hash: str | None,
        judge: dict[str, Any],
    ) -> StoreOutcome: ...


def web_base_url() -> str:
    return (os.environ.get(WEB_URL_ENV, "").strip() or PRODUCTION_WEB).rstrip("/")


def fit_provenance(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """``record`` as the store will take it: whole when it fits the store's
    64 KiB cap, else its essential keys, plus a note saying so."""
    if record is None:
        return None
    if len(_compact(record).encode("utf-8")) <= MAX_PROVENANCE_BYTES:
        return record
    slim = {k: record[k] for k in _ESSENTIAL if k in record}
    slim["truncated"] = "the full sidecar exceeds the store's 64 KiB provenance cap"
    return slim


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _refusal_code(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("error"), str):
        return f"HTTP {resp.status_code} {detail['error']}"
    return f"HTTP {resp.status_code}"


class HttpImageStore:
    def __init__(
        self,
        repo: str,
        base_url: str | None = None,
        coord_url: str | None = None,
        http: httpx.Client | None = None,
        jwt_file: Path | None = None,
        runner: Callable[[], str] = runner_url,
    ) -> None:
        self.repo = repo
        self.base_url = (base_url or web_base_url()).rstrip("/")
        self.coord_url = (coord_url or coord_base_url()).rstrip("/")
        self.http = http or httpx.Client(timeout=TIMEOUT_S)
        refusal = None
        if self.base_url != PRODUCTION_WEB or self.coord_url != PRODUCTION_COORD:
            refusal = (
                f"the runner mints for production, and {self.base_url} / {self.coord_url} "
                "is not the production web API and coord"
            )
        self.credentials = DeviceCredentials(self.http, refusal, jwt_file=jwt_file, runner=runner)
        self._proof: OwnerProof | None = None

    def _owner(self) -> Credential | StoreOutcome:
        if not https_or_loopback(self.base_url):
            return StoreOutcome(
                "refused", f"{self.base_url} is neither https nor loopback; no token is sent"
            )
        if self._proof is None:
            self._proof = prove_owner(self.http, self.coord_url, self.repo, self.credentials)
        if self._proof.credential is None:
            return StoreOutcome("refused", f"no write: {self._proof.note}")
        return self._proof.credential

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response | StoreOutcome:
        cred = self._owner()
        if isinstance(cred, StoreOutcome):
            return cred
        url = f"{self.base_url}{path}"
        try:
            resp = self.http.request(
                method, url, headers={"Authorization": f"Bearer {cred.token}"}, **kwargs
            )
        except httpx.HTTPError as e:
            return StoreOutcome("unknown", f"{method} {url}: {type(e).__name__}")
        if resp.status_code in (401, 403):
            # The proven owner's credential is the only one a write may use.
            return StoreOutcome(
                "unknown", f"{method} {url} answered HTTP {resp.status_code} to {cred.source}"
            )
        return resp

    def upload(
        self, path: Path, kind: AssetKind, provenance: dict[str, Any] | None
    ) -> StoreOutcome:
        sha = sha256_file(path)
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            return StoreOutcome("refused", f"{path.name} is {size} bytes, over the store's cap")
        data: dict[str, str] = {"kind": kind}
        fitted = fit_provenance(provenance)
        if fitted is not None:
            data["provenance"] = _compact(fitted)
        answer = self._send(
            "POST",
            UPLOAD_PATH,
            data=data,
            files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
        )
        if isinstance(answer, StoreOutcome):
            return answer
        if answer.status_code not in (200, 201):
            status: OutcomeStatus = "refused" if 400 <= answer.status_code < 500 else "unknown"
            return StoreOutcome(status, _refusal_code(answer))
        try:
            body = answer.json()
        except ValueError:
            return StoreOutcome("unknown", "the store answered an unreadable body")
        if not isinstance(body, dict) or body.get("sha256") != sha:
            return StoreOutcome("unknown", "the store's answer does not name these bytes' sha256")
        conflicts = body.get("conflicts") or []
        note = f" ({len(conflicts)} provenance conflict(s) kept as stored)" if conflicts else ""
        if answer.status_code == 201:
            return StoreOutcome("stored", f"stored as {kind}{note}")
        return StoreOutcome("existing", f"already stored (kind {body.get('kind')}){note}")

    def propose(
        self,
        *,
        repo: str,
        slot_id: str,
        asset_sha256: str,
        status: CandidateStatus,
        recipe_hash: str | None,
        judge: dict[str, Any],
    ) -> StoreOutcome:
        payload = {
            "repo": repo,
            "slot_id": slot_id,
            "asset_sha256": asset_sha256,
            "status": status,
            "recipe_hash": recipe_hash,
            "judge": judge,
        }
        answer = self._send("PUT", CANDIDATES_PATH, json=payload)
        if isinstance(answer, StoreOutcome):
            return answer
        if answer.status_code not in (200, 201):
            outcome: OutcomeStatus = "refused" if 400 <= answer.status_code < 500 else "unknown"
            return StoreOutcome(outcome, _refusal_code(answer))
        try:
            body = answer.json()
        except ValueError:
            return StoreOutcome("unknown", "the store answered an unreadable body")
        cand = body.get("candidate") if isinstance(body, dict) else None
        if (
            not isinstance(cand, dict)
            or cand.get("asset_sha256") != asset_sha256
            or cand.get("status") != status
        ):
            return StoreOutcome("unknown", "the store's answer does not read back this proposal")
        demoted = body.get("demoted") or []
        note = f"; {len(demoted)} earlier choice(s) returned to candidate" if demoted else ""
        return StoreOutcome("proposed", f"{status} for {repo} {slot_id}{note}")
