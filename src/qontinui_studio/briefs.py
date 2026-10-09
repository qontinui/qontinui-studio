"""briefs/*.yaml — what to generate, for which slots, from which providers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qontinui_studio.errors import ConfigError
from qontinui_studio.providers import ProviderInventory
from qontinui_studio.sites import Site


class Target(BaseModel):
    """Either a site slot (``site`` + ``slot``) or a campaign (``campaign``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str | None = None
    slot: str | None = None
    campaign: str | None = None

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> Self:
        site_slot = self.site is not None and self.slot is not None and self.campaign is None
        campaign = self.campaign is not None and self.site is None and self.slot is None
        if not (site_slot or campaign):
            raise ValueError("a target is either {site, slot} or {campaign}")
        return self

    @property
    def is_slot(self) -> bool:
        return self.site is not None


class Brief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", description="the asset id")
    purpose: str = Field(min_length=1)
    targets: list[Target] = Field(min_length=1)
    aspect_ratio: str
    prompt: str = Field(min_length=1)
    negative_prompt: str | None = None
    style_guide: Path | None = None
    reference_images: list[Path] = Field(default_factory=list)
    provider_preference: list[str] = Field(min_length=1)
    variants: int = Field(default=1, ge=1, le=8)

    @field_validator("aspect_ratio")
    @classmethod
    def _ratio(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() and int(p) > 0 for p in parts):
            raise ValueError(f"aspect_ratio must look like '16:9', got '{v}'")
        return v

    @property
    def ratio(self) -> float:
        w, h = self.aspect_ratio.split(":")
        return int(w) / int(h)

    def full_prompt(self) -> str:
        """The prompt actually sent: brief prompt plus the style guide text."""
        parts = [self.prompt.strip()]
        if self.style_guide is not None:
            parts.append("Style guide:\n" + self.style_guide.read_text(encoding="utf-8").strip())
        if self.negative_prompt:
            parts.append(f"Avoid: {self.negative_prompt.strip()}")
        return "\n\n".join(parts)


class LoadedBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    brief: Brief
    path: Path
    sha256: str


def load_brief(path: Path) -> LoadedBrief:
    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")
    base = path.parent
    if isinstance(raw.get("style_guide"), str):
        raw["style_guide"] = str((base / raw["style_guide"]).resolve())
    if isinstance(raw.get("reference_images"), list):
        raw["reference_images"] = [
            str((base / p).resolve()) if isinstance(p, str) else p for p in raw["reference_images"]
        ]
    try:
        brief = Brief.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
    return LoadedBrief(
        brief=brief, path=path.resolve(), sha256=hashlib.sha256(text.encode()).hexdigest()
    )


def validate_brief(brief: Brief, sites: dict[str, Site], providers: ProviderInventory) -> list[str]:
    """Cross-file validation. Returns every problem found (empty == valid)."""
    problems: list[str] = []
    for t in brief.targets:
        if not t.is_slot:
            continue
        site = sites.get(t.site or "")
        if site is None:
            problems.append(f"target site '{t.site}' is not defined under sites/")
        elif t.slot not in site.slots:
            problems.append(f"site '{t.site}' has no slot '{t.slot}'")
    known = {p.id for p in providers.providers}
    for pid in brief.provider_preference:
        if pid not in known:
            problems.append(f"provider_preference names unknown provider '{pid}'")
    if brief.style_guide is not None and not brief.style_guide.is_file():
        problems.append(f"style_guide {brief.style_guide} does not exist")
    for ref in brief.reference_images:
        if not ref.is_file():
            problems.append(f"reference image {ref} does not exist")
    if not any(
        providers.get(pid).schedulable_reason() is None
        for pid in brief.provider_preference
        if pid in known
    ):
        reasons = {
            pid: providers.get(pid).schedulable_reason()
            for pid in brief.provider_preference
            if pid in known
        }
        problems.append(f"no preferred provider is schedulable: {reasons}")
    return problems
