"""providers.yaml — the per-provider terms inventory (plan Phase 0, D4).

Each row records what the terms review found, with a source link per verdict.
``unknown`` is a first-class answer and it keeps the adapter OFF: D4 says an
adapter runs only where the provider's terms allow automated use, and an
unconfirmed answer is not an allowance.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qontinui_studio.errors import ConfigError

Verdict = Literal["yes", "no", "unknown"]
Media = Literal["image", "video"]
AdapterKind = Literal["gemini_api", "openai_images", "fake", "none"]


def _coerce_verdict(value: Any) -> Any:
    # PyYAML (YAML 1.1) reads a bare `yes`/`no` as a bool. Accept that rather
    # than make every row's author remember to quote.
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return value


def _default_media() -> list[Media]:
    return ["image"]


class SourcedVerdict(BaseModel):
    """A yes/no/unknown answer and the URL it was read from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Verdict
    source: str | None = None
    quote: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _bool_to_verdict(cls, v: Any) -> Any:
        return _coerce_verdict(v)

    @model_validator(mode="after")
    def _yes_no_needs_a_source(self) -> SourcedVerdict:
        if self.value != "unknown" and not self.source:
            raise ValueError(f"a '{self.value}' verdict must cite a source URL")
        return self


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
    # The per-UTC-day cap the scheduler will use. ``None`` means no free quota
    # is recorded, so the scheduler never assigns work to this provider —
    # spending money is an operator decision, not a scheduler default.
    daily_quota: int | None = Field(default=None, ge=0)
    tos_reviewed: date | None = None
    sources: list[str] = Field(default_factory=list)
    notes: str | None = None

    @property
    def licence_reviewed(self) -> bool:
        return self.tos_reviewed is not None and self.commercial_use.value != "unknown"

    def adapter_gate(self) -> str | None:
        """Return why D4 keeps this provider's adapter off, or ``None`` when it may run."""
        # Terms first: they are the reason an adapter may not exist at all.
        if self.tos_reviewed is None:
            return "terms not reviewed (tos_reviewed is unset)"
        if self.automation_allowed.value != "yes":
            return f"automation_allowed is '{self.automation_allowed.value}'"
        if self.adapter == "none":
            return "no adapter exists for this provider"
        if self.adapter in ("gemini_api", "openai_images") and self.api_available.value != "yes":
            return f"api_available is '{self.api_available.value}'"
        if self.adapter != "fake" and not self.model:
            return "no model configured"
        return None

    def schedulable_reason(self) -> str | None:
        """Why the scheduler must not assign work here, or ``None``."""
        gate = self.adapter_gate()
        if gate is not None:
            return gate
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
        seen: set[str] = set()
        for p in self.providers:
            if p.id in seen:
                raise ValueError(
                    f"provider '{p.id}' appears twice — D4 allows one account per provider; "
                    "multiple accounts are never used to get around a provider's limits"
                )
            seen.add(p.id)
        return self

    def get(self, provider_id: str) -> Provider:
        for p in self.providers:
            if p.id == provider_id:
                return p
        raise ConfigError(f"unknown provider '{provider_id}'")


def load_providers(path: Path) -> ProviderInventory:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        return ProviderInventory.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
