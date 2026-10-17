"""providers.yaml — the per-provider terms inventory (plan Phase 0, D4).

Each row records what the terms review found, with a source link per verdict.
``unknown`` is a first-class answer and it keeps the adapter OFF: D4 says an
adapter runs only where the provider's terms allow automated use, and an
unconfirmed answer is not an allowance.
"""

from __future__ import annotations

import ipaddress
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qontinui_studio.errors import ConfigError

Verdict = Literal["yes", "no", "unknown"]
Media = Literal["image", "video"]
AdapterKind = Literal[
    "gemini_api", "openai_images", "openai_compatible_images", "ui_bridge_wrapper", "fake", "none"
]
# API adapters: an adapter that needs ``api_available: yes`` and is at most one
# row per kind (one account per provider, D4).
API_ADAPTERS = ("gemini_api", "openai_images")
# How the scheduler bounds a provider: ``daily_quota`` (a recorded free quota
# per day; none recorded means no work) or ``unmetered`` (a self-hosted host on
# the local network that costs nothing per image and bounds itself with typed
# ``busy``/``deferred`` refusals — plan 2026-10-13 Phase 11 step 2).
Capacity = Literal["daily_quota", "unmetered"]
UNMETERED_ADAPTERS = ("openai_compatible_images",)
# A UI Bridge wrapper driving the operator's own web account, spawned by the
# runner on the operator's machine. It runs only under an ``operator_override``
# (plan 2026-10-17-free-image-generation-local-gpu-and-private-ui-bridge-wrappers D2).
WRAPPER_ADAPTERS = ("ui_bridge_wrapper",)
# The manual-equivalent ceiling on an override's daily cap and its pace floor;
# the private wrappers hold the same numbers (web-image-core index.ts).
MAX_PER_DAY_CEILING = 25
MIN_INTERVAL_FLOOR_S = 30


# Literal address ranges that count as "this machine or the local network".
LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",  # loopback
        "::1/128",
        "10.0.0.0/8",  # RFC 1918
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",  # link-local
        "fe80::/10",
        "fc00::/7",  # IPv6 unique-local
        "100.64.0.0/10",  # CGNAT, where Tailscale addresses live
        "fd7a:115c:a1e0::/48",  # Tailscale's IPv6 range
    )
)
# Hostnames an operator vouches for (e.g. a MagicDNS short name), comma-separated.
LOCAL_HOSTS_ENV = "STUDIO_LOCAL_NETWORK_HOSTS"


def is_local_network(url: str, *, named_hosts: bool = True) -> bool:
    """Whether ``url``'s host is on this machine or the local network: a
    LITERAL loopback, RFC 1918, link-local, IPv6 unique-local or CGNAT/Tailscale
    IP, ``localhost``, or a hostname listed in ``$STUDIO_LOCAL_NETWORK_HOSTS``.
    No other name passes — a name can resolve anywhere.

    This is an ACCIDENT GUARD against billing, not a security boundary: it stops
    an ``unmetered`` row (or a gateway route it would ride) from being pointed
    at a public, possibly billed host by mistake. Anyone who can edit
    providers.yaml or the environment can still point it anywhere.
    ``named_hosts=False`` ignores the allow-list (literal addresses and
    ``localhost`` only)."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        allowed = {
            h.strip().lower().rstrip(".")
            for h in os.environ.get(LOCAL_HOSTS_ENV, "").split(",")
            if h.strip()
        }
        return host == "localhost" or (named_hosts and host in allowed)
    return any(ip in net for net in LOCAL_NETWORKS if net.version == ip.version)


def _coerce_verdict(value: Any) -> Any:
    # PyYAML (YAML 1.1) reads a bare `yes`/`no` as a bool. Accept that rather
    # than make every row's author remember to quote.
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return value


# Third-party copies of terms pages. A verdict must be read from the provider's
# own page; a mirror can be stale or altered, so it may be cited in a note but
# never as a verdict's source.
MIRROR_DOMAINS = frozenset(
    {
        "open.windriver.com",
        "tosdr.org",
        "conductatlas.com",
        "termsfeed.com",
        "archive.ph",
        "archive.today",
    }
)


def _check_first_party(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if not url.startswith("https://") or not host:
        raise ValueError(f"source must be an https URL, got '{url}'")
    if any(host == d or host.endswith("." + d) for d in MIRROR_DOMAINS):
        raise ValueError(f"'{host}' is a third-party mirror, not a first-party terms page")
    return url


def _default_media() -> list[Media]:
    return ["image"]


class SourcedVerdict(BaseModel):
    """A yes/no/unknown answer and the URL it was read from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Verdict
    source: str | None = None
    # ``quote`` is VERBATIM text from ``source``; anything paraphrased or
    # second-hand goes in ``note``.
    quote: str | None = None
    note: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _bool_to_verdict(cls, v: Any) -> Any:
        return _coerce_verdict(v)

    @field_validator("source")
    @classmethod
    def _first_party(cls, v: str | None) -> str | None:
        return None if v is None else _check_first_party(v)

    @model_validator(mode="after")
    def _every_verdict_needs_a_source(self) -> SourcedVerdict:
        # Every verdict cites the page that was checked — for ``unknown`` the
        # page that could not be read or did not answer, with a ``note`` saying so.
        if not self.source:
            raise ValueError(f"a '{self.value}' verdict must cite the source URL that was checked")
        if self.value == "unknown" and not self.note:
            raise ValueError(
                "an 'unknown' verdict needs a note saying why it could not be confirmed"
            )
        return self


class OperatorOverride(BaseModel):
    """The operator's dated decision to drive a provider whose terms say
    ``automation_allowed: no``. The terms verdict stays on the row unchanged
    (every asset's provenance still records it); this block records the
    decision and the bounds that keep the use at manual volume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decided: date
    # Where the decision is written down (a plan path or a coord record id).
    record: str = Field(min_length=1)
    # The manual-equivalent daily cap. The row's ``daily_quota`` must equal it.
    max_per_day: int = Field(ge=1, le=MAX_PER_DAY_CEILING)
    # Human pace: the least time between two prompts, enforced by the wrapper.
    min_interval_s: int = Field(ge=MIN_INTERVAL_FLOOR_S)
    # A login wall, CAPTCHA or bot challenge stops the run and hands it to the
    # operator. No other value exists: nothing ever tries to get past one.
    on_challenge: Literal["stop_and_hand_off"]


class Provider(BaseModel):
    """One provider row. One row == one account (D4: one account per provider)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    provider: str
    plan: str
    api_available: SourcedVerdict
    automation_allowed: SourcedVerdict
    commercial_use: SourcedVerdict
    watermark: str
    watermark_visible: bool | None = None
    media: list[Media] = Field(default_factory=_default_media)
    adapter: AdapterKind = "none"
    model: str | None = None
    api_key_env: str | None = None
    # The API base URL. ``None`` is the adapter's vendor default. For the
    # local provider it is the facade's address (loopback on spaceship), and
    # ``base_url_env`` names an env var that overrides it per machine.
    base_url: str | None = None
    base_url_env: str | None = None
    capacity: Capacity = "daily_quota"
    # The per-UTC-day cap the scheduler will use. ``None`` means no free quota
    # is recorded, so the scheduler never assigns work to this provider —
    # spending money is an operator decision, not a scheduler default.
    daily_quota: int | None = Field(default=None, ge=0)
    # The IANA timezone whose midnight resets the provider's daily quota (the
    # quota "day" is counted in it). Gemini resets at midnight Pacific.
    quota_reset_tz: str = "UTC"
    tos_reviewed: date | None = None
    # The provider's own web domains: ``host`` (matches the host and its
    # subdomains) or ``host/path-prefix`` for a first-party page on shared
    # hosting (e.g. ``intercom.help/leonardo-ai``). Every verdict source and
    # every entry of ``sources`` must sit under one of them — the primary
    # first-party check; the mirror denylist is a second line.
    domains: list[str] = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)
    notes: str | None = None
    # The runner wrapper id a ``ui_bridge_wrapper`` row dispatches to.
    wrapper_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    operator_override: OperatorOverride | None = None

    @field_validator("base_url")
    @classmethod
    def _base_url_shape(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"https?://[^\s/]+(/\S*)?", v):
            raise ValueError(f"base_url must be an http(s) URL, got '{v}'")
        return v

    @model_validator(mode="after")
    def _capacity_rules(self) -> Provider:
        if self.capacity == "unmetered":
            if self.adapter not in UNMETERED_ADAPTERS:
                raise ValueError(
                    f"provider '{self.id}': capacity 'unmetered' is only for a self-hosted "
                    f"adapter {list(UNMETERED_ADAPTERS)}, not '{self.adapter}'"
                )
            if self.daily_quota is not None:
                raise ValueError(
                    f"provider '{self.id}': an unmetered provider records no daily_quota"
                )
        if self.adapter == "openai_compatible_images" and not (self.base_url or self.base_url_env):
            raise ValueError(f"provider '{self.id}': openai_compatible_images needs a base_url")
        if self.operator_override is not None:
            if self.adapter not in WRAPPER_ADAPTERS:
                raise ValueError(
                    f"provider '{self.id}': an operator_override applies only to a "
                    f"{list(WRAPPER_ADAPTERS)} adapter, not '{self.adapter}'"
                )
            if self.daily_quota != self.operator_override.max_per_day:
                raise ValueError(
                    f"provider '{self.id}': daily_quota ({self.daily_quota}) must equal "
                    f"operator_override.max_per_day ({self.operator_override.max_per_day})"
                )
        if self.adapter in WRAPPER_ADAPTERS:
            if self.operator_override is None:
                # Even with automation_allowed "yes": the override carries the
                # manual-volume bounds every dispatch sends.
                raise ValueError(
                    f"provider '{self.id}': a ui_bridge_wrapper row needs an operator_override "
                    "(its bounds ride every dispatch)"
                )
            if not self.wrapper_id:
                raise ValueError(f"provider '{self.id}': ui_bridge_wrapper needs a wrapper_id")
            if self.capacity == "unmetered":
                raise ValueError(f"provider '{self.id}': a ui_bridge_wrapper is never unmetered")
        elif self.wrapper_id is not None:
            raise ValueError(f"provider '{self.id}': wrapper_id is only for ui_bridge_wrapper")
        return self

    def resolved_base_url(self) -> str | None:
        """The base URL in effect on this machine: ``base_url_env`` when that
        variable is set, else ``base_url``."""
        if self.base_url_env:
            override = os.environ.get(self.base_url_env, "").strip()
            if override:
                return override
        return self.base_url

    @field_validator("quota_reset_tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone '{v}'") from e
        return v

    @field_validator("domains")
    @classmethod
    def _domain_shape(cls, v: list[str]) -> list[str]:
        for d in v:
            if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(/[A-Za-z0-9._~/-]+)?", d):
                raise ValueError(f"bad provider domain '{d}' (host or host/path-prefix)")
        return v

    def owns(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        for d in self.domains:
            dhost, _, prefix = d.partition("/")
            if host != dhost and not host.endswith("." + dhost):
                continue
            if not prefix or path == "/" + prefix or path.startswith("/" + prefix + "/"):
                return True
        return False

    @model_validator(mode="after")
    def _sources_are_the_providers_own(self) -> Provider:
        urls = [
            v.source
            for v in (self.api_available, self.automation_allowed, self.commercial_use)
            if v.source
        ] + list(self.sources)
        for url in urls:
            if not self.owns(url):
                raise ValueError(
                    f"provider '{self.id}': source {url} is not on a declared provider domain "
                    f"{self.domains}"
                )
        return self

    @field_validator("sources")
    @classmethod
    def _sources_first_party(cls, v: list[str]) -> list[str]:
        return [_check_first_party(u) for u in v]

    def adapter_gate(self) -> str | None:
        """Return why D4 keeps this provider's adapter off, or ``None`` when it may run."""
        # Terms first: they are the reason an adapter may not exist at all.
        if self.tos_reviewed is None:
            return "terms not reviewed (tos_reviewed is unset)"
        if self.automation_allowed.value != "yes" and not self.overridden():
            return f"automation_allowed is '{self.automation_allowed.value}'"
        if self.adapter == "none":
            return "no adapter exists for this provider"
        if (
            self.adapter in (*API_ADAPTERS, *UNMETERED_ADAPTERS)
            and self.api_available.value != "yes"
        ):
            return f"api_available is '{self.api_available.value}'"
        if self.adapter != "fake" and not self.model:
            return "no model configured"
        return None

    def overridden(self) -> bool:
        """Whether the operator's override lets this row's adapter run despite
        its terms verdict: only a wrapper row, only with the bounds present."""
        return self.adapter in WRAPPER_ADAPTERS and self.operator_override is not None

    def schedulable_reason(self) -> str | None:
        """Why the scheduler must not assign work here, or ``None``."""
        gate = self.adapter_gate()
        if gate is not None:
            return gate
        if self.capacity == "unmetered":
            base = self.resolved_base_url() or ""
            if not is_local_network(base):
                return (
                    f"unmetered capacity needs a local-network base URL; '{base}' is not one "
                    "(a public host could bill per image)"
                )
            return None
        if not self.daily_quota:
            return "no free daily quota recorded (daily_quota unset or 0)"
        return None


class ProviderInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    note: str
    operator_accounts_owed: bool
    providers: list[Provider]

    @model_validator(mode="after")
    def _one_account_per_provider(self) -> ProviderInventory:
        # One account per provider, checked three ways: the row id, the
        # provider's (normalised) name, and the API key env var — two rows that
        # differ only in id or spelling would still be two accounts.
        # Also: at most one row per API adapter kind, and no web domain shared
        # between rows (one provider, one row).
        seen: dict[str, dict[str, str]] = {
            "id": {},
            "provider": {},
            "api_key_env": {},
            "adapter": {},
            "domain": {},
        }
        for p in self.providers:
            keys: list[tuple[str, str]] = [("id", p.id), ("provider", _normalise(p.provider))]
            if p.api_key_env:
                keys.append(("api_key_env", p.api_key_env))
            if p.adapter in API_ADAPTERS:
                keys.append(("adapter", p.adapter))
            keys += [("domain", d.partition("/")[0] + "/" + d.partition("/")[2]) for d in p.domains]
            for kind, key in keys:
                if key in seen[kind]:
                    raise ValueError(
                        f"providers '{seen[kind][key]}' and '{p.id}' share {kind} '{key}' — D4 "
                        "allows one account per provider; multiple accounts are never used to "
                        "get around a provider's limits"
                    )
                seen[kind][key] = p.id
        return self

    def adapter_kinds(self) -> dict[str, str]:
        """provider id -> adapter kind, for the store's inventory backfills."""
        return {p.id: p.adapter for p in self.providers}

    def get(self, provider_id: str) -> Provider:
        for p in self.providers:
            if p.id == provider_id:
                return p
        raise ConfigError(f"unknown provider '{provider_id}'")


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def load_providers(path: Path) -> ProviderInventory:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        return ProviderInventory.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
