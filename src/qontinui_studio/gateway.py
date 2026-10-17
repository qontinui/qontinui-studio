"""Gateway tenants: image calls go through the declared model gateway, or not at
all (decision record ``gateway-tenants-route-every-model-call-through-the-gateway``;
plan 2026-10-13 Phase 11 step 3).

The runner owns the declaration (``model_gateway::state()``: ``NotDeclared |
Declared | Unknown``, qontinui-runner#2138). Studio reads it over the runner's
HTTP API, ``GET {runner}/model-gateway`` — a read #2138 does not ship yet (it
exposes the state only as the Tauri command ``get_model_gateway``); it was
proposed to that plan's Phase 9 by coord finding. The expected answer, bare or
inside the runner's ``{"data": ...}`` envelope::

    {"state": "not_declared" | "declared" | "unknown",
     "reason": "...",
     "routes": {"images": "<base URL of an OpenAI-images-wire route>",
                "images_credential_env": "QONTINUI_GATEWAY_<NAME>"}}

The read is unauthenticated, so it is trusted only from a LOOPBACK runner
(``$QONTINUI_RUNNER_URL`` naming another host reads as ``unknown``), and only
after ``GET {runner}/health`` IDENTIFIES a qontinui runner build (a JSON body
carrying a ``buildId`` and a hex ``gitSha``). That is identification, not
authentication: any local process can answer the same shape, and loopback is
the only boundary (a FRAGILE HANDOFF, recorded in the plan's Phase 11 handoff).

A route is used only when it is https, or plain http to a LITERAL local
address. Through a route, adapters authenticate with the gateway's own
credential, read from an environment variable whose NAME the declaration
gives. Studio accepts only a name matching ``QONTINUI_GATEWAY_[A-Z0-9_]+``
that is not any inventory row's ``api_key_env`` (checked again when adapters
are built, where the inventory is known); any other name refuses every image
call. So no vendor key the inventory names — and no variable outside that
prefix, such as ``OPENAI_API_KEY`` or ``AWS_SECRET_ACCESS_KEY`` — is ever sent
to a gateway route. An adapter that needs a key is refused when the gateway
declares none.

How each reading resolves:

* the runner's port REFUSES the connection -> ``not_declared`` (no runner on
  the box means no declared gateway);
* a connect TIMEOUT, or any other transport failure -> ``unknown`` (a runner
  saturated past its accept backlog still exists);
* ``/health`` answers but does not identify a qontinui runner build ->
  ``unknown`` (``unidentified_runner``);
* ``/model-gateway`` ``404`` from an IDENTIFIED runner -> ``not_declared``: the
  build serves no gateway read, and the only build that can declare a gateway
  (#2138) is the one the read is proposed into. TRIPWIRE: once
  qontinui-runner#2138 merges, a 404 from a build whose ``/health`` ``gitSha``
  descends from #2138's merge commit is a gateway-capable build lacking the
  read, and must read ``unknown`` — compare ``gitSha`` against that merge
  commit here (#2138 is unmerged today, so there is no commit to compare
  against, and studio holds no runner checkout);
* ``declared`` with ``routes.images`` -> image calls go through that route
  (every adapter speaking the OpenAI images wire is pointed at it; any other
  wire is unavailable);
* ``declared`` without it, ``unknown``, a non-404 error status, an unreadable
  answer, an unacceptable route or credential name -> the typed refusal
  ``gateway_has_no_image_route``, and no vendor or local call is made.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from qontinui_studio.errors import GatewayRefusal, omitted
from qontinui_studio.providers import is_local_network

RUNNER_URL_ENV = "QONTINUI_RUNNER_URL"
# The runner binds IPv4 loopback only; ``localhost`` would pay a doomed ::1 connect.
DEFAULT_RUNNER_URL = "http://127.0.0.1:9876"
# ``/health`` has been sampled at 10 s on a loaded box; the timeout sits past it.
READ_TIMEOUT_S = 15.0
# The only environment-variable names a gateway may nominate as its credential.
GATEWAY_CREDENTIAL_ENV = re.compile(r"QONTINUI_GATEWAY_[A-Z0-9_]+")
_GIT_SHA = re.compile(r"[0-9a-f]{7,40}")

State = Literal["not_declared", "declared", "unknown"]


@dataclass(frozen=True)
class GatewayState:
    state: State
    # Where the reading came from (``runner``, ``no_runner``, ``route_absent``,
    # ``runner_unresponsive``, ``unidentified_runner``, ``unreadable``,
    # ``unacceptable_route``, ``unacceptable_credential_env``, ...), so a report
    # can say how it knows.
    source: str
    reason: str = ""
    images_base_url: str | None = None
    # The env var holding the GATEWAY's own credential for its images route,
    # when the declaration names one: always a ``QONTINUI_GATEWAY_*`` name.
    images_credential_env: str | None = None
    # The identified runner build's ``gitSha`` (for the #2138 tripwire above).
    runner_git_sha: str | None = None

    def refusal(self) -> GatewayRefusal | None:
        """The typed refusal for this state, or None when image calls may proceed."""
        if self.state == "not_declared":
            return None
        if self.state == "declared" and self.images_base_url:
            return None
        if self.state == "declared":
            why = "the declared model gateway names no images-capable route"
        else:
            # ``source`` is a closed code set; ``reason`` can carry runner text
            # (a body's reason, a state value, an httpx error), so it is never
            # printed — only its digest.
            why = (
                "the runner cannot say whether a model gateway is declared "
                f"({self.source}; {omitted(self.reason)})"
            )
        return GatewayRefusal(
            f"{GatewayRefusal.code}: {why}; no direct vendor or local image call is made",
            self.state,
        )


GatewayReader = Callable[[], GatewayState]


def runner_url() -> str:
    return os.environ.get(RUNNER_URL_ENV, "").strip() or DEFAULT_RUNNER_URL


def route_acceptable(url: str) -> bool:
    """A gateway images route studio will send requests to: https anywhere, or
    plain http only to a LITERAL local address (loopback, RFC 1918, link-local,
    ULA, CGNAT/Tailscale) or ``localhost`` — never plain http to a name."""
    scheme = urlparse(url).scheme.lower()
    if scheme == "https":
        return bool(urlparse(url).hostname)
    return scheme == "http" and is_local_network(url, named_hosts=False)


def credential_env_acceptable(name: str, vendor_envs: frozenset[str] = frozenset()) -> bool:
    """Whether a gateway may nominate ``name`` as its credential variable: a
    ``QONTINUI_GATEWAY_*`` name that is not one of the inventory's vendor keys."""
    return GATEWAY_CREDENTIAL_ENV.fullmatch(name) is not None and name not in vendor_envs


def runner_identity(body: object) -> str | None:
    """The ``gitSha`` of a qontinui runner build's ``/health`` body (top level or
    inside its ``data`` envelope, beside a non-empty ``buildId``), else None."""
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    scopes: list[dict[str, Any]] = [body, data] if isinstance(data, dict) else [body]
    sha: str | None = next(
        (str(d["gitSha"]) for d in scopes if isinstance(d.get("gitSha"), str)), None
    )
    build = next((str(d["buildId"]) for d in scopes if isinstance(d.get("buildId"), str)), "")
    if sha is None or not build.strip() or not _GIT_SHA.fullmatch(sha):
        return None
    return sha


def read_gateway_state(base: str | None = None, http: httpx.Client | None = None) -> GatewayState:
    """Read this install's model-gateway state from the local runner. The read
    is unauthenticated, so it is trusted only from a LOOPBACK runner whose
    ``/health`` identifies a qontinui runner build: a runner URL pointing
    anywhere else, or an unidentified answer, reads as ``unknown`` and refuses."""
    root = (base or runner_url()).rstrip("/")
    host = (urlparse(root).hostname or "").lower()
    if host != "localhost" and not _is_loopback_literal(host):
        return GatewayState(
            "unknown",
            "untrusted_runner_url",
            f"runner URL {root} is not loopback; an unauthenticated gateway read is "
            "trusted only from this machine's runner",
        )
    client = http or httpx.Client(timeout=READ_TIMEOUT_S)
    try:
        return _read(root, client)
    finally:
        if http is None:
            client.close()


def _get(client: httpx.Client, url: str) -> httpx.Response | GatewayState:
    """``GET url``, or the state its transport failure means. Only a REFUSED
    connection means no runner; a connect timeout is a saturated runner, which
    exists (its state is unknown)."""
    try:
        return client.get(url)
    except httpx.ConnectTimeout as e:
        return GatewayState(
            "unknown", "runner_unresponsive", f"GET {url} timed out connecting: {e}"
        )
    except httpx.ConnectError as e:
        return GatewayState("not_declared", "no_runner", f"no runner at {url}: {e}")
    except httpx.TransportError as e:
        return GatewayState("unknown", "unreadable", f"GET {url}: {e}")


def _read(root: str, client: httpx.Client) -> GatewayState:
    health = _get(client, f"{root}/health")
    if isinstance(health, GatewayState):
        return health
    try:
        sha = runner_identity(health.json()) if health.status_code == 200 else None
    except ValueError:
        sha = None
    if sha is None:
        return GatewayState(
            "unknown",
            "unidentified_runner",
            f"GET {root}/health answered HTTP {health.status_code} without a qontinui "
            "runner build's identity (buildId, gitSha)",
        )
    url = f"{root}/model-gateway"
    resp = _get(client, url)
    if isinstance(resp, GatewayState):
        # The runner answered /health a moment ago: a refusal now is not "no runner".
        return GatewayState("unknown", "unreadable", resp.reason, runner_git_sha=sha)
    if resp.status_code == 404:
        # TRIPWIRE (qontinui-runner#2138): see the module docstring — once #2138
        # merges, a 404 from a build at or past its merge commit reads unknown.
        return GatewayState(
            "not_declared",
            "route_absent",
            f"GET {url} answered 404: runner build {sha} serves no gateway read",
            runner_git_sha=sha,
        )
    if resp.status_code != 200:
        return GatewayState(
            "unknown",
            "unreadable",
            f"GET {url} answered HTTP {resp.status_code}",
            runner_git_sha=sha,
        )
    try:
        body = resp.json()
    except ValueError as e:
        return GatewayState(
            "unknown", "unreadable", f"GET {url}: unreadable body: {e}", runner_git_sha=sha
        )
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    state = body.get("state") if isinstance(body, dict) else None
    if state not in ("not_declared", "declared", "unknown"):
        return GatewayState(
            "unknown",
            "unreadable",
            f"GET {url}: no recognised 'state' ({state!r})",
            runner_git_sha=sha,
        )
    assert isinstance(body, dict)
    routes = body.get("routes")
    images = routes.get("images") if isinstance(routes, dict) else None
    cred = routes.get("images_credential_env") if isinstance(routes, dict) else None
    if isinstance(images, str) and images and not route_acceptable(images):
        return GatewayState(
            "unknown",
            "unacceptable_route",
            f"the declared images route {images} is neither https nor a literal local address",
            runner_git_sha=sha,
        )
    if cred is not None and not (isinstance(cred, str) and credential_env_acceptable(cred)):
        return GatewayState(
            "unknown",
            "unacceptable_credential_env",
            f"the declared images credential variable {cred!r} is not a "
            f"{GATEWAY_CREDENTIAL_ENV.pattern} name; no other variable is ever sent to a route",
            runner_git_sha=sha,
        )
    return GatewayState(
        state,
        "runner",
        str(body.get("reason") or ""),
        images if isinstance(images, str) and images else None,
        cred,
        sha,
    )


def _is_loopback_literal(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False
