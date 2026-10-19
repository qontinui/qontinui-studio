"""What the fleet's own image facade says it serves, read before generating.

The facade (qontinui ``docker/imagegen``, plan Phase 10) answers ``GET
/qontinui/status``. A facade that serves a ``recipes`` map (``{<recipe id>:
<sha256>}``, the hash ``recipe_hash(recipe, manifest)`` it runs each recipe
under) lets ``studio compile`` see a recipe mismatch BEFORE spending a
generation: the facade still serving a recipe the licence manifest has moved
past (or one it has moved ahead of). An older facade serves no map; the
answer is then UNKNOWN (None) and the compile falls back to checking each
candidate's own provenance.

Behind llama-swap the facade is reached through the passthrough
``/upstream/<model>/qontinui/status``; a provider pointed straight at the
facade answers ``/qontinui/status`` itself. Both are tried, in that order.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import quote

import httpx

from qontinui_studio.providers import Provider

STATUS_PATH = "/qontinui/status"
TIMEOUT_S = 15.0
_SHA = re.compile(r"^[0-9a-f]{64}$")

FacadeProbe = Callable[[Provider], "dict[str, str] | None"]


def _roots(provider: Provider) -> list[str]:
    base = (provider.resolved_base_url() or "").rstrip("/")
    if not base:
        return []
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    model = quote(provider.model or "", safe="")
    return [f"{root}/upstream/{model}{STATUS_PATH}", f"{root}{STATUS_PATH}"]


class HttpFacadeProbe:
    """Reads a facade provider's served recipe map; None when the facade
    serves none (an older facade) or does not answer (UNKNOWN)."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=TIMEOUT_S)

    def __call__(self, provider: Provider) -> dict[str, str] | None:
        for url in _roots(provider):
            try:
                resp = self.http.get(url)
            except httpx.HTTPError:
                return None
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                return None
            try:
                body = resp.json()
            except ValueError:
                return None
            recipes = body.get("recipes") if isinstance(body, dict) else None
            if not isinstance(recipes, dict):
                return None  # an older facade: no map served
            return {
                str(k): v for k, v in recipes.items() if isinstance(v, str) and _SHA.fullmatch(v)
            }
        return None
