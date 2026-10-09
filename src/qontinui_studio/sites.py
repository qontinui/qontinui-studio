"""sites/*.yaml — publication targets and their slots.

A site declares what KIND of site it is (the D6 rules key off it), whether it
is commercial (the D5 licence rule keys off it), and the exact pixel size and
content category of every slot a brief may target.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

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

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
PageHealthPolicy = Literal["required", "waived"]

DEFAULT_ILLUSTRATIVE_LABEL = "Illustrative image (AI-generated)"
DEFAULT_AI_LABEL = "AI-generated image"


def _default_formats() -> list[ExportFormat]:
    return ["webp"]


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    # Required. A Site fills ``decorative`` for an omitted category on demo and
    # product sites; a real business must state every slot's category, because
    # D6 refuses AI food/premises imagery there and a silent default would
    # disable that check.
    category: SlotCategory
    formats: list[ExportFormat] = Field(default_factory=_default_formats, min_length=1)
    remove_background: bool = False
    focal_x: float = Field(default=0.5, ge=0.0, le=1.0)
    focal_y: float = Field(default=0.5, ge=0.0, le=1.0)


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ID_PATTERN)
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
    # http://127.0.0.1:9876/ui-bridge/control/page-health. Unset with
    # ``page_health: required`` -> the D7 gate REFUSES publication.
    page_health_url: str | None = None
    # D7 runs against a PREVIEW, before the live swap: the studio writes the
    # site's live files plus this batch's files and merged manifest into
    # ``preview_dir`` (served by the preview whose page ``page_health_url``
    # checks), then fetches ``preview_manifest_url`` to confirm the preview
    # actually serves this batch's manifest (by sha256). Required with
    # ``page_health_url``.
    preview_dir: Path | None = None
    preview_manifest_url: str | None = None
    # ``waived`` is allowed only on a demo site (one with no running preview
    # yet); the operator notice prints the waiver on every publication.
    page_health: PageHealthPolicy = "required"
    slots: dict[str, Slot] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _slot_categories(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("slots"), dict):
            return data
        kind = data.get("kind")
        slots: dict[str, Any] = {}
        for name, slot in data["slots"].items():
            if isinstance(slot, dict) and "category" not in slot:
                if kind == "real_business":
                    raise ValueError(
                        f"slot '{name}': a real_business site must state every slot's category"
                    )
                slot = {**slot, "category": "decorative"}
            slots[name] = slot
        return {**data, "slots": slots}

    @model_validator(mode="after")
    def _checks(self) -> Site:
        for name in self.slots:
            if not re.fullmatch(ID_PATTERN, name):
                raise ValueError(f"bad slot name '{name}' (must match {ID_PATTERN})")
        if self.page_health == "waived" and self.kind != "demo":
            raise ValueError("page_health: waived is allowed only on a demo site")
        if self.page_health_url and not (self.preview_dir and self.preview_manifest_url):
            raise ValueError(
                "page_health_url needs preview_dir and preview_manifest_url (D7 checks a "
                "preview of this batch before the live swap)"
            )
        if (
            self.preview_dir is not None
            and self.preview_dir.resolve() == self.publish_dir.resolve()
        ):
            raise ValueError("preview_dir must differ from publish_dir")
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
    for key in ("publish_dir", "preview_dir"):
        value = raw.get(key)
        if isinstance(value, str) and not Path(value).is_absolute():
            raw[key] = str((path.parent / value).resolve())
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
