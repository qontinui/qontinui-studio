"""The fleet's device-JWT chain, as studio uses it for every factory door it
calls with a device credential: coord's ``image_generation`` dial (Phase 12),
coord's prompt documents (the imagery treatment a slot names) and
qontinui-web's content-addressed image store (Phase 13).

Order: ``$COORD_DEVICE_JWT``, then ``~/.qontinui/coord-device-jwt``, then a
mint from the local runner's UI Bridge (``POST
/ui-bridge/invoke/get_coord_device_token``). It falls through on VALIDITY, not
presence: a token whose ``exp`` has passed is skipped, and the caller passes
over one its door answers 401/403 to. The runner mints for PRODUCTION only, so
a minted token is offered only to a production door; and with
``$STUDIO_COORD_TENANT_ID`` set, a minted token whose ``tenant_id`` claim names
another tenant is discarded (a device bound to several tenants writes only as
the tenant that owns the repo).

Tokens are never printed or logged; a skipped source is described by where it
came from, never by its value, and a token is only ever sent to an https or a
loopback door (:func:`https_or_loopback`).

A WRITE keyed by a repository goes only to the tenant PROVEN to own it
(served policy ``coordination`` ``repo-writes-go-to-the-owning-tenant``;
the fleet's ``ctc_owner_tenant``). :func:`prove_owner` is that proof: under a
token whose ``tenant_id`` claim is T, coord's ``GET /pr-merge/<owner%2Fname>/0/
author-session`` answers 200 only when T owns the repo and 404 when it does not
(or there is no such repo); any other answer proves nothing, and the walk stops
UNKNOWN.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from qontinui_studio.gateway import runner_url

DEVICE_JWT_ENV = "COORD_DEVICE_JWT"
DEVICE_JWT_FILE = Path("~/.qontinui/coord-device-jwt")
# Sent with the runner mint when set: a runner bound to several tenants answers
# 409 ``tenant_required`` to a mint that names none.
TENANT_ENV = "STUDIO_COORD_TENANT_ID"
RUNNER_MINT_PATH = "/ui-bridge/invoke/get_coord_device_token"
OWNER_PROBE_PATH = "/pr-merge/{repo}/0/author-session"


@dataclass(frozen=True)
class Credential:
    source: str
    token: str


def jwt_claims(token: str) -> dict[str, object] | None:
    """The payload of a three-part JWT, or None when it does not decode. Not
    verified: the door verifies; studio only reads ``exp`` and ``tenant_id``."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def jwt_expired(token: str, now_epoch: float | None = None) -> bool:
    """Whether ``token``'s ``exp`` claim has passed. An ``exp`` that is present
    but not a finite number (a string, a bool, NaN, infinity) counts as
    expired. A token whose payload does not decode, or that carries no
    ``exp``, is not known to be expired: the door decides it."""
    claims = jwt_claims(token)
    if claims is None or "exp" not in claims:
        return False
    exp = claims["exp"]
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        return True
    return exp <= (time.time() if now_epoch is None else now_epoch)


def is_loopback_url(url: str) -> bool:
    """A LITERAL loopback address, or exactly ``localhost``. A name that merely
    starts with ``127.`` (``127.evil.example``) resolves anywhere."""
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def https_or_loopback(url: str) -> bool:
    """Whether a bearer token may be sent to ``url``: https, or a literal
    loopback / ``localhost`` http door."""
    scheme = urlparse(url).scheme.lower()
    return scheme == "https" or (scheme == "http" and is_loopback_url(url))


@dataclass(frozen=True)
class OwnerProof:
    """The credential PROVEN to act for the tenant that owns a repo, or None
    with why not (refuted for every credential, or UNKNOWN)."""

    credential: Credential | None
    note: str


def prove_owner(
    http: httpx.Client, coord_base: str, repo: str, chain: DeviceCredentials
) -> OwnerProof:
    """Walk ``chain`` until a credential's tenant is proven to own ``repo``
    (``owner/name``, exact spelling). A credential coord refuses, or whose
    tenant it refutes, is passed over; any other answer stops the walk
    UNKNOWN — never "not the owner", never "the owner"."""
    base = coord_base.rstrip("/")
    if "/" not in repo:
        return OwnerProof(None, f"'{repo}' is not an owner/name slug; no tenant can own it")
    if not https_or_loopback(base):
        return OwnerProof(None, f"coord {base} is neither https nor loopback; no token is sent")
    url = base + OWNER_PROBE_PATH.format(repo=quote(repo, safe=""))
    skipped: list[str] = []
    passed: list[str] = []
    for cred in chain.credentials(skipped):
        try:
            resp = http.get(url, headers={"Authorization": f"Bearer {cred.token}"})
        except httpx.HTTPError as e:
            return OwnerProof(None, f"the owner probe did not answer ({type(e).__name__}): UNKNOWN")
        claims = jwt_claims(cred.token) or {}
        tenant = claims.get("tenant_id")
        if resp.status_code == 200:
            return OwnerProof(cred, f"{cred.source} (tenant {tenant}) is proven to own {repo}")
        if resp.status_code == 404:
            passed.append(f"{cred.source}: tenant {tenant} does not own {repo}")
            continue
        if resp.status_code in (401, 403):
            passed.append(f"{cred.source}: coord answered HTTP {resp.status_code}")
            continue
        return OwnerProof(
            None, f"the owner probe answered HTTP {resp.status_code}; the owner is UNKNOWN"
        )
    return OwnerProof(
        None,
        f"no credential is proven to act for the tenant that owns {repo} ("
        + "; ".join([*skipped, *passed])
        + ")",
    )


class DeviceCredentials:
    """The chain, for one door. ``mint_refusal`` says why the runner mint may
    not be offered to this door (None: it may — the door is production)."""

    def __init__(
        self,
        http: httpx.Client,
        mint_refusal: str | None,
        jwt_file: Path | None = None,
        runner: Callable[[], str] = runner_url,
    ) -> None:
        self.http = http
        self.mint_refusal = mint_refusal
        self.jwt_file = (jwt_file or DEVICE_JWT_FILE).expanduser()
        self.runner = runner

    def __iter__(self) -> Iterator[Credential]:
        return self.credentials([])

    def credentials(self, skipped: list[str]) -> Iterator[Credential]:
        """Each usable credential in order; why each other source was passed
        over is appended to ``skipped``. The runner mint runs only when the
        static sources are spent."""
        env = os.environ.get(DEVICE_JWT_ENV, "").strip()
        if not env:
            skipped.append(f"${DEVICE_JWT_ENV} unset")
        elif jwt_expired(env):
            skipped.append(f"${DEVICE_JWT_ENV} expired")
        else:
            yield Credential(f"${DEVICE_JWT_ENV}", env)
        try:
            file_token = self.jwt_file.read_text(encoding="utf-8").strip()
        except OSError:
            file_token = ""
        if not file_token:
            skipped.append(f"{self.jwt_file} absent or empty")
        elif jwt_expired(file_token):
            skipped.append(f"{self.jwt_file} expired")
        else:
            yield Credential(str(self.jwt_file), file_token)
        minted = self._runner_mint()
        if isinstance(minted, Credential):
            yield minted
        else:
            skipped.append(minted)

    def _runner_mint(self) -> Credential | str:
        if self.mint_refusal is not None:
            return f"runner mint not used: {self.mint_refusal}"
        root = self.runner().rstrip("/")
        if not is_loopback_url(root):
            return f"runner mint not used: runner URL {root} is not loopback"
        tenant = os.environ.get(TENANT_ENV, "").strip()
        args: dict[str, str] = {"tenantId": tenant} if tenant else {}
        try:
            resp = self.http.post(f"{root}{RUNNER_MINT_PATH}", json={"args": args})
        except httpx.HTTPError as e:
            return f"runner mint: {type(e).__name__}: {e}"
        if resp.status_code != 200:
            return f"runner mint answered HTTP {resp.status_code}"
        try:
            body = resp.json()
        except ValueError:
            return "runner mint: unreadable answer"
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, str) or not data.strip() or " " in data.strip():
            return "runner mint: the runner holds no device credential (no token in its answer)"
        token = data.strip()
        if jwt_expired(token):
            return "runner mint: the minted token is expired"
        if tenant:
            claims = jwt_claims(token) or {}
            claim = claims.get("tenant_id")
            if not isinstance(claim, str) or claim.lower() != tenant.lower():
                return (
                    f"runner mint: the minted token's tenant_id claim {claim!r} is not "
                    f"${TENANT_ENV} ({tenant}); discarded"
                )
        return Credential("runner mint", token)
