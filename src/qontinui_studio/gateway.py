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
     "routes": {"images": "<base URL of an OpenAI-images-wire route>"}}

How each reading resolves:

* no runner listening -> ``not_declared`` (no runner on the box means no
  declared gateway);
* ``404`` -> ``not_declared``: the runner build serves no gateway read, and the
  only build that can declare a gateway (#2138) is the one the read is
  proposed into. FRAGILE if #2138 lands without the read — see the finding;
* ``declared`` with ``routes.images`` -> image calls go through that route
  (every adapter speaking the OpenAI images wire is pointed at it; any other
  wire is unavailable);
* ``declared`` without it, ``unknown``, a 5xx, a timeout or an unreadable
  answer -> the typed refusal ``gateway_has_no_image_route``, and no vendor or
  local call is made.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import httpx

from qontinui_studio.errors import GatewayRefusal

RUNNER_URL_ENV = "QONTINUI_RUNNER_URL"
# The runner binds IPv4 loopback only; ``localhost`` would pay a doomed ::1 connect.
DEFAULT_RUNNER_URL = "http://127.0.0.1:9876"
READ_TIMEOUT_S = 15.0

State = Literal["not_declared", "declared", "unknown"]


@dataclass(frozen=True)
class GatewayState:
    state: State
    # Where the reading came from (``runner``, ``no_runner``, ``route_absent``,
    # ``unreadable``), so a report can say how it knows.
    source: str
    reason: str = ""
    images_base_url: str | None = None

    def refusal(self) -> GatewayRefusal | None:
        """The typed refusal for this state, or None when image calls may proceed."""
        if self.state == "not_declared":
            return None
        if self.state == "declared" and self.images_base_url:
            return None
        if self.state == "declared":
            why = "the declared model gateway names no images-capable route"
        else:
            why = f"the runner cannot say whether a model gateway is declared ({self.reason})"
        return GatewayRefusal(
            f"{GatewayRefusal.code}: {why}; no direct vendor or local image call is made",
            self.state,
        )


GatewayReader = Callable[[], GatewayState]


def runner_url() -> str:
    return os.environ.get(RUNNER_URL_ENV, "").strip() or DEFAULT_RUNNER_URL


def read_gateway_state(base: str | None = None, http: httpx.Client | None = None) -> GatewayState:
    """Read this install's model-gateway state from the local runner."""
    url = f"{(base or runner_url()).rstrip('/')}/model-gateway"
    client = http or httpx.Client(timeout=READ_TIMEOUT_S)
    try:
        resp = client.get(url)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        return GatewayState("not_declared", "no_runner", f"no runner at {url}: {e}")
    except httpx.TransportError as e:
        return GatewayState("unknown", "unreadable", f"GET {url}: {e}")
    finally:
        if http is None:
            client.close()
    if resp.status_code == 404:
        return GatewayState(
            "not_declared",
            "route_absent",
            f"GET {url} answered 404: this runner build serves no gateway read",
        )
    if resp.status_code != 200:
        return GatewayState("unknown", "unreadable", f"GET {url} answered HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as e:
        return GatewayState("unknown", "unreadable", f"GET {url}: unreadable body: {e}")
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    state = body.get("state") if isinstance(body, dict) else None
    if state not in ("not_declared", "declared", "unknown"):
        return GatewayState(
            "unknown", "unreadable", f"GET {url}: no recognised 'state' ({state!r})"
        )
    assert isinstance(body, dict)
    routes = body.get("routes")
    images = routes.get("images") if isinstance(routes, dict) else None
    return GatewayState(
        state,
        "runner",
        str(body.get("reason") or ""),
        images if isinstance(images, str) and images.startswith(("http://", "https://")) else None,
    )
