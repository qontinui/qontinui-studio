"""sites/*.yaml — publication targets and their slots.

A site declares what KIND of site it is (the D6 rules key off it), whether it
is commercial (the D5 licence rule keys off it), and the exact pixel size and
content category of every slot a brief may target.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qontinui_studio.errors import ConfigError

SiteKind = Literal["demo", "real_business", "product"]
# `food` / `premises` matter for D6 on a real business; `product_screenshot` is
# never AI (screenshots come from the running product via UI Bridge).
SlotCategory = Literal[
    "decorative", "illustration", "food", "premises", "people", "product_screenshot"
]
ExportFormat = Literal["webp", "avif", "png", "jpeg"]

DEFAULT_ILLUSTRATIVE_LABEL = "Illustrative image (AI-generated)"
DEFAULT_AI_LABEL = "AI-generated image"


def _default_formats() -> list[ExportFormat]:
    return ["webp"]


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    category: SlotCategory = "decorative"
    formats: list[ExportFormat] = Field(default_factory=_default_formats, min_length=1)
    remove_background: bool = False
    focal_x: float = Field(default=0.5, ge=0.0, le=1.0)
    focal_y: float = Field(default=0.5, ge=0.0, le=1.0)


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    kind: SiteKind
    commercial: bool
    publish_dir: Path
    public_url: str | None = None
    # The label a demo/template site must show beside AI imagery (D6). On other
    # site kinds AI imagery still carries ``ai_label`` (EU synthetic-content
    # transparency).
    illustrative_label: str = DEFAULT_ILLUSTRATIVE_LABEL
    ai_label: str = DEFAULT_AI_LABEL
    # UI Bridge page-health endpoint for a running preview of this site, e.g.
    # http://127.0.0.1:9876/ui-bridge/control/page-health. Unset -> the D7
    # page-health gate is reported NOT_CONFIGURED for this site.
    page_health_url: str | None = None
    slots: dict[str, Slot] = Field(min_length=1)

    @model_validator(mode="after")
    def _slot_names(self) -> Site:
        for name in self.slots:
            if not name or "/" in name:
                raise ValueError(f"bad slot name '{name}'")
        return self

    def label_for_ai(self) -> str:
        return self.illustrative_label if self.kind == "demo" else self.ai_label


def load_site(path: Path) -> Site:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")
    publish_dir = raw.get("publish_dir")
    if isinstance(publish_dir, str) and not Path(publish_dir).is_absolute():
        raw["publish_dir"] = str((path.parent / publish_dir).resolve())
    try:
        return Site.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_sites(directory: Path) -> dict[str, Site]:
    sites: dict[str, Site] = {}
    if not directory.is_dir():
        return sites
    for path in sorted(directory.glob("*.yaml")):
        site = load_site(path)
        if site.id in sites:
            raise ConfigError(f"duplicate site id '{site.id}' ({path})")
        sites[site.id] = site
    return sites
