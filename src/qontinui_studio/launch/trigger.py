"""The launch trigger: no poller. A coord gate on the portfolio work unit,

    {"kind": "unit_status", "work_unit_id": <uuid>, "status": "shipped"}

with a ``run_skill`` continuation, clears when the unit ships and spawns an
agent session that runs the ``launch-kit`` skill (``.claude/skills/launch-kit``)
in qontinui-studio, which runs ``studio launch run`` for the product.

Registration, over coord's device/agent-authed doors (bearer from
``STUDIO_COORD_BEARER`` / ``STUDIO_COORD_BEARER_FILE``):

1. ``GET /coord/agent-work-units/<slug>`` — the device/agent READ door:
   resolves the ``work_unit_id`` without writing anything (an unknown slug is
   a 404 and a typed refusal; registration never creates or touches a unit);
2. ``POST /coord/work-units/<slug>/register-gate`` — returns the ``gate_id``;
3. ``GET /coord/agent-gates?work_unit_id=<uuid>`` — the read-back: the gate is
   ``registered`` only when the listing carries that ``gate_id``; a read that
   fails or omits it is reported, never assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx

from qontinui_studio.commands import shell_join
from qontinui_studio.errors import StudioError
from qontinui_studio.notify import BEARER_ENV, BEARER_FILE_ENV, COORD_BASE, resolve_bearer

SKILL = "launch-kit"
#: The only hosts a coord bearer is sent to (https only).
COORD_HOSTS: frozenset[str] = frozenset({"coord.qontinui.io"})
PHASE_NAME = "launch-kit"


class TriggerError(StudioError):
    """Registration could not be completed. ``stage`` names the step."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage


@dataclass(frozen=True)
class TriggerResult:
    work_unit_id: str
    gate_id: str
    read_back: Literal["confirmed", "absent", "unreadable"]
    read_back_detail: str
    initial_verdict: str | None = None
    warnings: tuple[str, ...] = ()


def gate_body(work_unit_id: str, product_slug: str) -> dict[str, Any]:
    return {
        "predicate": {"kind": "unit_status", "work_unit_id": work_unit_id, "status": "shipped"},
        "phase_name": PHASE_NAME,
        "continuation_spawn": {
            "action": "run_skill",
            "skill": SKILL,
            "args": [product_slug],
            "hint": (
                f"The work unit for portfolio product '{product_slug}' shipped. Run the qontinui-studio "
                "launch kit for it: `"
                + shell_join(
                    ["poetry", "run", "studio", "launch", "run", f"products/{product_slug}.yaml"]
                )
                + "`."
            ),
            "repos": ["qontinui-studio"],
        },
        "gate_class": "launch-kit",
    }


def _bearer(bearer: str | None) -> str:
    token = bearer or resolve_bearer()
    if not token:
        raise TriggerError("credential", f"no coord bearer: set {BEARER_ENV} or {BEARER_FILE_ENV}")
    return token


def _post(
    http: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str], stage: str
) -> dict[str, Any]:
    try:
        resp = http.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise TriggerError(stage, f"transport failure: {e}") from e
    if resp.status_code >= 300:
        raise TriggerError(stage, f"HTTP {resp.status_code}: {resp.text[:400]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise TriggerError(stage, "non-JSON answer") from e
    if not isinstance(data, dict):
        raise TriggerError(stage, "answer is not an object")
    return data


def register_trigger(
    work_unit_slug: str,
    product_slug: str,
    bearer: str | None = None,
    base_url: str = COORD_BASE,
    http: httpx.Client | None = None,
    allowed_hosts: frozenset[str] = COORD_HOSTS,
) -> TriggerResult:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in allowed_hosts:
        # The coord bearer is a credential: it goes only over https to coord.
        raise TriggerError(
            "credential",
            f"refusing coord base {base_url!r}: the bearer is sent only over https to "
            f"{sorted(allowed_hosts)}",
        )
    client = http or httpx.Client(timeout=30.0)
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {_bearer(bearer)}"}
    slug_path = quote(work_unit_slug, safe="")

    try:
        resp = client.get(f"{base}/coord/agent-work-units/{slug_path}", headers=headers)
    except httpx.HTTPError as e:
        raise TriggerError("resolve", f"transport failure: {e}") from e
    if resp.status_code == 404:
        raise TriggerError(
            "resolve",
            f"no work unit '{work_unit_slug}' in this tenant — register the plan first; "
            "the trigger never creates one",
        )
    if resp.status_code != 200:
        raise TriggerError("resolve", f"HTTP {resp.status_code}: {resp.text[:400]}")
    try:
        unit = resp.json().get("work_unit")
    except (ValueError, AttributeError) as e:
        raise TriggerError("resolve", "non-JSON answer") from e
    work_unit_id = unit.get("id") if isinstance(unit, dict) else None
    if not isinstance(work_unit_id, str) or not work_unit_id:
        raise TriggerError("resolve", "answer carries no work_unit.id")

    registered = _post(
        client,
        f"{base}/coord/work-units/{slug_path}/register-gate",
        gate_body(work_unit_id, product_slug),
        headers,
        "register-gate",
    )
    gate_id = registered.get("gate_id")
    if not isinstance(gate_id, str) or not gate_id:
        raise TriggerError("register-gate", f"answer carries no gate_id: {registered}")
    warnings = tuple(str(w) for w in registered.get("warnings") or [])
    verdict = registered.get("initial_verdict")

    read_back: Literal["confirmed", "absent", "unreadable"]
    try:
        resp = client.get(
            f"{base}/coord/agent-gates", params={"work_unit_id": work_unit_id}, headers=headers
        )
        if resp.status_code != 200:
            read_back, detail = "unreadable", f"HTTP {resp.status_code}: {resp.text[:200]}"
        else:
            page = resp.json()
            gates = page.get("gates") if isinstance(page, dict) else None
            if not isinstance(gates, list):
                read_back, detail = "unreadable", "listing has no 'gates' array"
            elif any(isinstance(g, dict) and g.get("gate_id") == gate_id for g in gates):
                read_back, detail = (
                    "confirmed",
                    f"gate {gate_id} listed on work unit {work_unit_id}",
                )
            else:
                truncated = isinstance(page, dict) and page.get("truncated")
                read_back = "unreadable" if truncated else "absent"
                detail = (
                    f"listing is truncated and does not show {gate_id}"
                    if truncated
                    else f"listing of {len(gates)} gate(s) does not carry {gate_id}"
                )
    except (httpx.HTTPError, ValueError) as e:
        read_back, detail = "unreadable", f"read-back failed: {e}"
    return TriggerResult(
        work_unit_id=work_unit_id,
        gate_id=gate_id,
        read_back=read_back,
        read_back_detail=detail,
        initial_verdict=str(verdict) if verdict is not None else None,
        warnings=warnings,
    )
