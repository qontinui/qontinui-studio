"""What the fleet's own image facade says it serves, read before generating —
without ever making llama-swap evict a resident model for the question.

The facade (qontinui ``docker/imagegen``, plan Phase 10) answers ``GET
/qontinui/status``. One that serves a ``recipes`` map (``{<recipe id>:
<sha256>}``, each the ``recipe_hash(recipe, manifest)`` it stamps at
``qontinui_provenance.recipe.hash``) lets ``studio compile`` see a recipe
mismatch before spending a generation. An older facade serves no map.

Behind llama-swap the facade is one model entry (``qontinui-imagegen``, the
recipe aliases its aliases). llama-swap keeps ONE resident model: reaching the
facade's status through ``/upstream/<model>/...`` starts the facade and evicts
whatever is resident (an LLM such as the gemma emitter). So the probe:

1. reads llama-swap's ``GET /running`` (which loads nothing) and ``GET
   /v1/models`` (the entry's ``meta.llamaswap.qontinui_capability`` names the
   image facade) first, and asks the facade only when its entry is RESIDENT
   and ready;
2. when it is not resident, wakes it ONLY if the caller says a generation is
   about to happen anyway (``wake``): an interactive job the facade is not
   holding off. A bulk run (the facade's bulk window may defer it) or a facade
   that answered ``busy``/``deferred`` is never woken for a probe;
3. answers each provider once per run (cached).

A provider pointed straight at the facade (no llama-swap: ``/running`` is a
404) is asked directly. Anything unreadable is UNKNOWN, never "agrees".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import quote

import httpx

from qontinui_studio.providers import Provider

STATUS_PATH = "/qontinui/status"
RUNNING_PATH = "/running"
MODELS_PATH = "/v1/models"
CAPABILITY = "image_generation"
#: A resident facade answers in well under this.
RESIDENT_TIMEOUT_S = 15.0
#: Waking the facade (llama-swap starts its process and waits for its health
#: check) takes longer; a generation would have paid it anyway.
WAKE_TIMEOUT_S = 180.0
LLAMA_SWAP_TIMEOUT_S = 10.0
_SHA = re.compile(r"^[0-9a-f]{64}$")

ServedState = Literal["map", "absent", "unknown"]


@dataclass(frozen=True)
class Served:
    """What a facade serves: its recipe map (``map``), no map at all
    (``absent``: an older facade), or ``unknown`` (not asked, or no answer)."""

    state: ServedState
    detail: str
    recipes: dict[str, str] = field(default_factory=dict)


class FacadeProbe(Protocol):
    def __call__(self, provider: Provider, *, wake: bool) -> Served: ...


def _root(provider: Provider) -> str | None:
    base = (provider.resolved_base_url() or "").rstrip("/")
    if not base:
        return None
    return base[: -len("/v1")] if base.endswith("/v1") else base


class HttpFacadeProbe:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=LLAMA_SWAP_TIMEOUT_S)
        self._cache: dict[str, Served] = {}
        self.calls: list[str] = []  # every URL asked, for the report and tests

    def __call__(self, provider: Provider, *, wake: bool) -> Served:
        cached = self._cache.get(provider.id)
        if cached is not None and (cached.state != "unknown" or not wake):
            return cached
        served = self._read(provider, wake)
        self._cache[provider.id] = served
        return served

    def _get(self, url: str, timeout: float) -> httpx.Response | None:
        self.calls.append(url)
        try:
            return self.http.get(url, timeout=timeout)
        except httpx.HTTPError:
            return None

    def _read(self, provider: Provider, wake: bool) -> Served:
        root = _root(provider)
        if root is None:
            return Served("unknown", "the provider names no base URL")
        running = self._get(root + RUNNING_PATH, LLAMA_SWAP_TIMEOUT_S)
        if running is None:
            return Served("unknown", "llama-swap /running did not answer")
        if running.status_code == 404:
            # No llama-swap in front: the facade itself answers.
            return self._status(root + STATUS_PATH, RESIDENT_TIMEOUT_S, "the facade")
        ready = _ready_models(running)
        if ready is None:
            return Served("unknown", "llama-swap /running answered no readable list")
        listed = self._facade_entries(root)
        entries = listed | ({provider.model} if provider.model else set())
        resident = sorted(ready & entries)
        if resident:
            url = f"{root}/upstream/{quote(resident[0], safe='')}{STATUS_PATH}"
            return self._status(url, RESIDENT_TIMEOUT_S, f"resident facade {resident[0]}")
        if not wake:
            return Served(
                "unknown",
                "the facade is not resident, and nothing about to run would load it; not "
                "woken for a probe",
            )
        if not entries:
            return Served("unknown", "llama-swap lists no image facade entry to wake")
        # The entry llama-swap lists as the facade, else the provider's model.
        target = sorted(listed)[0] if listed else sorted(entries)[0]
        url = f"{root}/upstream/{quote(target, safe='')}{STATUS_PATH}"
        return self._status(url, WAKE_TIMEOUT_S, f"facade {target}, woken for this generation")

    def _facade_entries(self, root: str) -> set[str]:
        """The model ids llama-swap lists as the image facade."""
        resp = self._get(root + MODELS_PATH, LLAMA_SWAP_TIMEOUT_S)
        if resp is None or resp.status_code != 200:
            return set()
        try:
            body = resp.json()
        except ValueError:
            return set()
        out: set[str] = set()
        for model in body.get("data", []) if isinstance(body, dict) else []:
            meta = model.get("meta") if isinstance(model, dict) else None
            swap = meta.get("llamaswap") if isinstance(meta, dict) else None
            if isinstance(swap, dict) and swap.get("qontinui_capability") == CAPABILITY:
                if isinstance(model.get("id"), str):
                    out.add(model["id"])
        return out

    def _status(self, url: str, timeout: float, who: str) -> Served:
        resp = self._get(url, timeout)
        if resp is None or resp.status_code != 200:
            code = "no answer" if resp is None else f"HTTP {resp.status_code}"
            return Served("unknown", f"{who}: /qontinui/status gave {code}")
        try:
            body = resp.json()
        except ValueError:
            return Served("unknown", f"{who}: /qontinui/status is unreadable")
        recipes = body.get("recipes") if isinstance(body, dict) else None
        if not isinstance(recipes, dict):
            return Served("absent", f"{who} serves no recipe map (an older facade)")
        return Served(
            "map",
            f"{who} serves its recipe map",
            {str(k): v for k, v in recipes.items() if isinstance(v, str) and _SHA.fullmatch(v)},
        )


def _ready_models(resp: httpx.Response) -> set[str] | None:
    """llama-swap's ``{"running": [{"model": id, "state": "ready", ...}]}``."""
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    running = body.get("running") if isinstance(body, dict) else None
    if not isinstance(running, list):
        return None
    return {
        str(r["model"])
        for r in running
        if isinstance(r, dict) and isinstance(r.get("model"), str) and r.get("state") == "ready"
    }
