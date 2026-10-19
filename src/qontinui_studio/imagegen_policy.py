"""The image-generation off-switches: coord's ``image_generation`` dial (per
tenant, with a per-consumer-repo override) and this machine's
``QONTINUI_IMAGEGEN=0`` (plan 2026-10-13 Phase 12 steps 3 and 4, D1.7).

Image generation ships ON; these switches turn it down. They are never the
authorization — the image facade's loopback bind is (plan D1.6).

Levels, as coord serves them (``qontinui-coord``
``crates/coord/src/image_generation_policy.rs``):

====================  ======================================================
``on``                every built provider may be used (a cloud one only
                      within its launch-kit quota, which the scheduler keeps)
``local_only``        only the fleet's OWN image provider: an unmetered
                      adapter on the local network, called directly. Every
                      cloud adapter, and every adapter riding a model-gateway
                      route, gets no NEW work — no new prompt or reference
                      leaves the fleet's hardware and no new quota is spent;
                      a job already submitted there may still be polled and
                      downloaded
``off``               no provider call at all; a typed refusal
====================  ======================================================

Where the level comes from, in order:

1. **The machine switch.** ``QONTINUI_IMAGEGEN=0`` is ``off`` with no coord
   read. ``1``, empty or unset leave the dial in charge. Any other value is
   ``off`` too, and says so: a mistyped off-switch must not silently read as on.
2. **coord.** ``GET {coord}/coord/fleet-policy?domain=image_generation[&repo=]``
   with a device JWT, naming the consumer repo (a site's ``repo``) so a
   per-repo override governs it; a site with no repo reads the tenant level.
   The credential chain is the fleet's: ``$COORD_DEVICE_JWT``, then
   ``~/.qontinui/coord-device-jwt``, then a mint from the local runner's UI
   Bridge (``POST /ui-bridge/invoke/get_coord_device_token``). It falls
   through on VALIDITY, not presence: a token whose ``exp`` has passed is
   skipped, and one coord answers 401/403 to is passed over for the next. A
   runner-minted token is sent to production coord only. Every answered level
   is saved as the repo's last-known level in the studio's own database.
3. **Unreadable** (no usable credential, transport failure, a non-200, an
   answer that is not one of the three levels): the last level coord answered
   for that repo; with none, ``local_only``. Cloud is the preference-sensitive
   axis — it costs money and sends data off the machine — so an unknown never
   renders as the ``on`` default ([policy: unknown-must-not-render-as-a-default]).

A batch targeting several sites takes the MOST restrictive of their levels.
A reading is reused for :data:`POLL_INTERVAL_S` and then read again. The
pipeline decides once before driving a batch and again before EVERY provider
call the scheduler makes (each submit and each poll/download), so a dial set
to ``off`` stops a running ``studio jobs run`` before its next provider call
once the cached reading is older than one poll.

Under ``local_only`` a job already submitted to a cloud provider may still be
polled and its result downloaded (``AdapterSet.poll_only``): no NEW prompt or
reference leaves the machine, and paid work already done is not thrown away.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import httpx

from qontinui_studio.adapters import AdapterSet
from qontinui_studio.device_auth import DEVICE_JWT_FILE, Credential, DeviceCredentials
from qontinui_studio.errors import ImageGenerationRefused
from qontinui_studio.gateway import runner_url
from qontinui_studio.providers import Provider, ProviderInventory, is_local_network

DOMAIN = "image_generation"
MACHINE_SWITCH_ENV = "QONTINUI_IMAGEGEN"
COORD_URL_ENV = "COORD_HTTP_URL"
PRODUCTION_COORD = "https://coord.qontinui.io"
FLEET_POLICY_PATH = "/coord/fleet-policy"
# How long one coord reading is reused before the dial is read again.
POLL_INTERVAL_S = 60.0
READ_TIMEOUT_S = 15.0

Level = Literal["on", "local_only", "off"]
LEVELS: tuple[Level, ...] = ("on", "local_only", "off")
_RANK: dict[str, int] = {"on": 0, "local_only": 1, "off": 2}
# The level an unreadable dial with no last-known value resolves to.
UNREADABLE_DEFAULT: Level = "local_only"

Source = Literal["machine_switch", "coord", "last_known", "unreadable"]


# ---------------------------------------------------------------------------
# One coord reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DialReading:
    """What one read of coord's dial produced: a level, or None with why not."""

    level: Level | None
    reason: str = ""
    # ``repo`` | ``tenant`` | ``system`` | ``none`` (coord's no-row default).
    resolved_scope: str | None = None
    # Which credential carried the read that answered.
    credential: str | None = None


class DialReader(Protocol):
    def __call__(self, repo: str | None) -> DialReading: ...


def coord_base_url() -> str:
    return (os.environ.get(COORD_URL_ENV, "").strip() or PRODUCTION_COORD).rstrip("/")


class CoordDialReader:
    """Reads the dial from coord with the fleet's device-JWT chain."""

    def __init__(
        self,
        base_url: str | None = None,
        http: httpx.Client | None = None,
        jwt_file: Path | None = None,
        runner: Callable[[], str] = runner_url,
    ) -> None:
        self.base_url = (base_url or coord_base_url()).rstrip("/")
        self.http = http or httpx.Client(timeout=READ_TIMEOUT_S)
        self.jwt_file = (jwt_file or DEVICE_JWT_FILE).expanduser()
        self.runner = runner

    # ---- the credential chain ------------------------------------------

    def credentials(self, skipped: list[str]) -> Iterator[Credential]:
        """Each usable credential in order (:mod:`qontinui_studio.device_auth`);
        the runner mint is offered to production coord only."""
        refusal = (
            None
            if self.base_url == PRODUCTION_COORD
            else f"the runner mints for production coord and {self.base_url} is not it"
        )
        chain = DeviceCredentials(self.http, refusal, jwt_file=self.jwt_file, runner=self.runner)
        return chain.credentials(skipped)

    # ---- the read -------------------------------------------------------

    def __call__(self, repo: str | None) -> DialReading:
        params = {"domain": DOMAIN, **({"repo": repo} if repo else {})}
        url = f"{self.base_url}{FLEET_POLICY_PATH}"
        skipped: list[str] = []
        refused: list[str] = []
        for cred in self.credentials(skipped):
            try:
                resp = self.http.get(
                    url, params=params, headers={"Authorization": f"Bearer {cred.token}"}
                )
            except httpx.HTTPError as e:
                return DialReading(None, f"GET {url}: {type(e).__name__}: {e}")
            if resp.status_code in (401, 403):
                refused.append(f"coord answered HTTP {resp.status_code} to {cred.source}")
                continue
            return _parse(resp, url, cred.source)
        return DialReading(
            None, "no usable device credential (" + "; ".join([*skipped, *refused]) + ")"
        )


def _parse(resp: httpx.Response, url: str, source: str) -> DialReading:
    if resp.status_code != 200:
        return DialReading(None, f"GET {url} answered HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as e:
        return DialReading(None, f"GET {url}: unreadable body: {e}")
    level = body.get("effective_level") if isinstance(body, dict) else None
    if level not in LEVELS:
        return DialReading(None, f"GET {url}: 'effective_level' {level!r} is not one of {LEVELS}")
    assert isinstance(body, dict)
    scope = body.get("resolved_scope")
    return DialReading(
        level,
        "coord answered",
        str(scope) if isinstance(scope, str) else None,
        source,
    )


def default_reader() -> DialReader:
    """The production reader. Looked up at call time, so tests replace it."""
    return CoordDialReader()


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    level: Level
    source: Source
    reason: str
    # The consumer repo it was decided for (None: the tenant-wide level).
    repo: str | None = None
    resolved_scope: str | None = None

    def describe(self) -> str:
        where = f" for {self.repo}" if self.repo else " (tenant-wide)"
        scope = f", {self.resolved_scope} scope" if self.resolved_scope else ""
        return f"{self.level}{where} [{self.source}{scope}]: {self.reason}"

    def refusal(self) -> ImageGenerationRefused | None:
        if self.level != "off":
            return None
        return ImageGenerationRefused(
            f"image_generation_off: image generation is off{self._where()} "
            f"({self.source}: {self.reason}); no provider call is made",
            "image_generation_off",
            self,
        )

    def _where(self) -> str:
        if self.source == "machine_switch":
            return " on this machine"
        return f" for {self.repo}" if self.repo else " for this tenant"


def machine_switch() -> PolicyDecision | None:
    """``QONTINUI_IMAGEGEN``'s verdict, or None when it leaves the dial in charge."""
    raw = os.environ.get(MACHINE_SWITCH_ENV)
    if raw is None or raw.strip() in ("", "1"):
        return None
    if raw.strip() == "0":
        return PolicyDecision("off", "machine_switch", f"{MACHINE_SWITCH_ENV}=0")
    return PolicyDecision(
        "off",
        "machine_switch",
        f"{MACHINE_SWITCH_ENV}={raw.strip()!r} is neither 0 nor 1; an unrecognised "
        "off-switch value is read as off",
    )


def _repo_key(repo: str | None) -> str:
    return (repo or "").lower()


def load_last_known(conn: sqlite3.Connection, repo: str | None) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT level, resolved_scope, read_at FROM imagegen_dial_last_known WHERE repo_key = ?",
        (_repo_key(repo),),
    ).fetchone()
    return row


def save_last_known(
    conn: sqlite3.Connection, repo: str | None, reading: DialReading, at: datetime
) -> None:
    conn.execute(
        "INSERT INTO imagegen_dial_last_known (repo_key, level, resolved_scope, read_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (repo_key) DO UPDATE SET level = excluded.level, "
        "resolved_scope = excluded.resolved_scope, read_at = excluded.read_at",
        (_repo_key(repo), reading.level, reading.resolved_scope, at.isoformat()),
    )
    conn.commit()


class ImagegenPolicy:
    """Decides the effective level for a set of consumer repos, reading coord
    at most once per :attr:`poll_interval_s` per repo."""

    def __init__(
        self, reader: DialReader | None = None, poll_interval_s: float = POLL_INTERVAL_S
    ) -> None:
        self._reader = reader
        self.poll_interval_s = poll_interval_s
        self._cache: dict[str, tuple[datetime, DialReading]] = {}

    @property
    def reader(self) -> DialReader:
        if self._reader is None:
            self._reader = default_reader()
        return self._reader

    def decide(
        self, conn: sqlite3.Connection, repos: Iterable[str | None], now: datetime
    ) -> PolicyDecision:
        """The most restrictive level across ``repos`` (an empty set reads the
        tenant-wide level). The machine switch wins without a coord read."""
        switched = machine_switch()
        if switched is not None:
            return switched
        keyed = {_repo_key(r): r for r in repos} or {"": None}
        decisions = [self._decide_one(conn, keyed[k], now) for k in sorted(keyed)]
        return max(decisions, key=lambda d: _RANK[d.level])

    def _decide_one(
        self, conn: sqlite3.Connection, repo: str | None, now: datetime
    ) -> PolicyDecision:
        key = _repo_key(repo)
        cached = self._cache.get(key)
        if cached is not None and (now - cached[0]).total_seconds() < self.poll_interval_s:
            reading = cached[1]
        else:
            reading = self.reader(repo)
            self._cache[key] = (now, reading)
            if reading.level is not None:
                save_last_known(conn, repo, reading, now)
        if reading.level is not None:
            via = f" with {reading.credential}" if reading.credential else ""
            return PolicyDecision(
                reading.level, "coord", f"{reading.reason}{via}", repo, reading.resolved_scope
            )
        last = load_last_known(conn, repo)
        if last is not None:
            return PolicyDecision(
                last["level"],
                "last_known",
                f"coord unreadable ({reading.reason}); using the level it last answered, "
                f"at {last['read_at']}",
                repo,
                last["resolved_scope"],
            )
        return PolicyDecision(
            UNREADABLE_DEFAULT,
            "unreadable",
            f"coord unreadable ({reading.reason}) and no level was ever read for "
            f"{repo or 'this tenant'}; an unknown dial is {UNREADABLE_DEFAULT}, never on",
            repo,
        )


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def is_fleet_provider(provider: Provider, route: str | None) -> bool:
    """Whether ``local_only`` admits this provider: the fleet's own image
    provider, called directly — an unmetered adapter whose base URL is on the
    local network — or the in-process ``fake`` adapter, which calls nothing.
    Nothing riding a gateway route is admitted, wherever the route points."""
    if route is not None:
        return False
    if provider.adapter == "fake":
        return True
    return provider.capacity == "unmetered" and is_local_network(provider.resolved_base_url() or "")


def restrict(
    adapters: AdapterSet,
    decision: PolicyDecision,
    inventory: ProviderInventory,
    preference: Iterable[str] = (),
) -> AdapterSet:
    """The adapters ``decision`` lets this batch use.

    ``off`` raises its refusal. ``local_only`` moves every adapter that is not
    the fleet's own out of ``adapters`` (no new submission) into ``poll_only``
    (an in-flight job there is still polled and downloaded, so paid work is not
    lost) and names why in ``unavailable``. It raises the typed
    ``image_generation_local_only`` refusal when that leaves none of
    ``preference`` (the brief's providers) while some of them were built, so the
    switch alone does not fail a batch's jobs for want of a provider. ``on``
    changes nothing."""
    refusal = decision.refusal()
    if refusal is not None:
        raise refusal
    if decision.level == "on":
        return adapters
    out = AdapterSet(
        unavailable=dict(adapters.unavailable),
        refusal=adapters.refusal,
        route=adapters.route,
        poll_only=dict(adapters.poll_only),
    )
    for pid, adapter in adapters.adapters.items():
        if is_fleet_provider(inventory.get(pid), adapters.route):
            out.adapters[pid] = adapter
        else:
            out.poll_only[pid] = adapter
            via = (
                f"through the gateway route {adapters.route}" if adapters.route else "a cloud host"
            )
            out.unavailable[pid] = (
                f"{pid}: image_generation is local_only ({decision.source}); this provider "
                f"calls {via}, not the fleet's own image host (in-flight jobs there are "
                "still collected)"
            )
    wanted = list(preference)
    if (
        wanted
        and any(p in adapters.adapters for p in wanted)
        and not any(p in out.adapters for p in wanted)
    ):
        raise ImageGenerationRefused(
            "image_generation_local_only: image generation is local_only"
            f"{decision._where()} ({decision.source}: {decision.reason}) and none of the "
            f"brief's providers {wanted} is the fleet's own image host; no provider call is made",
            "image_generation_local_only",
            decision,
        )
    return out
