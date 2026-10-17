"""briefs/*.yaml — what to generate, for which slots, from which providers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qontinui_studio import postprocess
from qontinui_studio.errors import ConfigError
from qontinui_studio.library import TAG_PATTERN, is_tag
from qontinui_studio.providers import ProviderInventory
from qontinui_studio.sites import ID_PATTERN, Site


class Target(BaseModel):
    """Either a site slot (``site`` + ``slot``) or a campaign (``campaign``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # All three become path components of variant/published files, so they
    # share the id pattern (no separators, no "..", no NUL).
    site: str | None = Field(default=None, pattern=ID_PATTERN)
    slot: str | None = Field(default=None, pattern=ID_PATTERN)
    campaign: str | None = Field(default=None, pattern=ID_PATTERN)

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

    id: str = Field(pattern=ID_PATTERN, description="the asset id")
    purpose: str = Field(min_length=1)
    targets: list[Target] = Field(min_length=1)
    aspect_ratio: str
    prompt: str = Field(min_length=1)
    negative_prompt: str | None = None
    style_guide: Path | None = None
    reference_images: list[Path] = Field(default_factory=list)
    provider_preference: list[str] = Field(min_length=1)
    variants: int = Field(default=1, ge=1, le=8)
    # Sent to providers whose wire takes them (the local facade); recorded in
    # every provenance sidecar.
    seed: int | None = Field(default=None, ge=0)
    recipe: str | None = Field(default=None, pattern=ID_PATTERN)
    # ``bulk`` work waits for the host's bulk window and never spills onto
    # another provider when the host refuses it.
    priority: Literal["interactive", "bulk"] = "interactive"
    # Library tags every asset this brief produces is filed under at ingest
    # (e.g. the brand reference set ``studio-style-v1``). A selection, not an
    # approval: approval stays a separate, recorded decision.
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, v: list[str]) -> list[str]:
        for t in v:
            if not is_tag(t):
                raise ValueError(f"tag '{t}' must match {TAG_PATTERN}")
        if len(set(v)) != len(v):
            raise ValueError("tags names a tag twice")
        return v

    @field_validator("targets")
    @classmethod
    def _unique_targets(cls, v: list[Target]) -> list[Target]:
        seen: set[tuple[str | None, str | None, str | None]] = set()
        for t in v:
            key = (t.site, t.slot, t.campaign)
            if key in seen:
                raise ValueError(f"duplicate target {t.model_dump(exclude_none=True)}")
            seen.add(key)
        return v

    @field_validator("provider_preference")
    @classmethod
    def _unique_providers(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("provider_preference names a provider twice")
        return v

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
        """The brief prompt plus the style guide text. The negative prompt is
        NOT folded in here: a wire with a negative-prompt field gets it there
        (folding ``Avoid: ...`` into a diffusion prompt pulls the model TOWARD
        the avoided concept); a wire without one folds it itself
        (``adapters.base.fold_negative``)."""
        parts = [self.prompt.strip()]
        if self.style_guide is not None:
            parts.append("Style guide:\n" + self.style_guide.read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)


class LoadedBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    brief: Brief
    path: Path
    sha256: str


def load_brief(path: Path, root: Path | None = None) -> LoadedBrief:
    """Load a brief. ``style_guide`` and ``reference_images`` resolve relative to
    the brief file and must stay inside the studio ``root`` (default: the
    directory above ``briefs/``, i.e. ``path.parent.parent``) — a brief cannot
    pull an arbitrary file from the machine into a provider request."""
    root = (root if root is not None else path.resolve().parent.parent).resolve()
    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")
    base = path.parent

    def confined(value: object) -> object:
        if not isinstance(value, str):
            return value
        resolved = (base / value).resolve()
        if not resolved.is_relative_to(root):
            raise ConfigError(f"{path}: '{value}' resolves outside the studio root {root}")
        return str(resolved)

    if raw.get("style_guide") is not None:
        raw["style_guide"] = confined(raw["style_guide"])
    if isinstance(raw.get("reference_images"), list):
        raw["reference_images"] = [confined(p) for p in raw["reference_images"]]
    try:
        brief = Brief.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
    return LoadedBrief(brief=brief, path=path.resolve(), sha256=_digest(text, brief))


def _digest(text: str, brief: Brief) -> str:
    """The batch hash: the brief text AND every file it pulls into the prompt
    (style guide, reference images), so editing any of them is a different
    brief. A missing file hashes as absent (validation reports it)."""
    h = hashlib.sha256()
    h.update(b"brief\0" + text.encode())
    files = ([brief.style_guide] if brief.style_guide else []) + list(brief.reference_images)
    for f in files:
        h.update(b"\0file\0" + str(f).encode() + b"\0")
        try:
            h.update(hashlib.sha256(f.read_bytes()).digest())
        except OSError:
            h.update(b"<absent>")
    return h.hexdigest()


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
        else:
            slot = site.slots[t.slot]
            # Pre-flight the local tools BEFORE any provider quota is spent.
            if slot.remove_background and not postprocess.rembg_available():
                problems.append(
                    f"{t.site}/{t.slot} needs background removal but 'rembg' is not installed "
                    "(poetry install -E bg)"
                )
            for fmt in slot.formats:
                if not postprocess.format_supported(fmt):
                    problems.append(f"{t.site}/{t.slot}: this Pillow build cannot encode '{fmt}'")
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
