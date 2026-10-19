"""``targets/*.yaml`` — a consumer repository as a publication target of
``studio compile`` (plan 2026-10-13 Phase 19 step 4).

A compile target names the repository (``owner/name``), where its slot specs
live inside a checkout (``slots_dir``, default ``frontend/image-slots``), where
the derivatives and ``studio-manifest.json`` land (``publish_dir``, default
``frontend/public/image-slots``), which providers generate, how many candidates,
and the site's D7 preview. It holds nothing about any one checkout: the
checkout is named on the command line (``--slots <checkout>/<slots_dir>``), and
:meth:`CompileTarget.site` turns the target plus that checkout plus its specs
into an ordinary :class:`~qontinui_studio.sites.Site` (``kind: product``,
``publish_dir`` inside the checkout, one site slot per compiled spec) — the
SAME publication target the asset pipeline publishes to, so the D5/D6/D7 gates,
the journalled swap and the manifest are the existing ones.

Step 2 — select before generating. Each spec is routed (:func:`route_for`):

=================  =========================================================
``lucide``         an icon: selected from lucide (the declared icon library,
                   ``domain_spec/visual-system`` "Imagery & iconography"),
                   never generated. Studio writes no file; the report names
                   the icon for the calling session.
``svg``            a simple illustration: agent-written inline SVG in
                   ``currentColor`` and token variables. The calling session
                   writes ``<slots_dir>/<slot>.svg``; studio routes and
                   validates it (:mod:`~qontinui_studio.compiler.authored`)
                   and writes nothing.
``diffusion``      rich illustration, photography, texture, scenes: N
                   candidates through the provider, AVIF + WebP + a PNG
                   fallback.
``traced``         flat raster art traced to SVG by vtracer (an ``svg``
                   derivative of a generated raster), with a PNG of the same
                   crop beside it.
=================  =========================================================

``logo`` and ``screenshot`` slots are never compiled: a logo is an identity
mark (a current binding of one may not be AI-generated) and a screenshot is
captured from the running product (D6). Every other role has a default route,
which a target may override per slot; the report says which applied.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qontinui_studio.compiler.specs import LoadedSpec, parse_aspect
from qontinui_studio.errors import ConfigError
from qontinui_studio.sites import (
    DEFAULT_AI_LABEL,
    ID_PATTERN,
    REPO_PATTERN,
    ExportFormat,
    Site,
    Slot,
    SlotCategory,
)

Route = Literal["lucide", "svg", "diffusion", "traced"]
ROUTES: tuple[Route, ...] = ("lucide", "svg", "diffusion", "traced")
# The routes whose output is a studio asset (derivatives + a manifest entry).
ASSET_ROUTES: tuple[Route, ...] = ("diffusion", "traced")

# Roles studio never compiles, and why (the report quotes the reason).
NOT_COMPILED: dict[str, str] = {
    "logo": "a logo is an identity mark: a current binding of a logo slot may not be "
    "AI-generated (ImageSlotProblem::AiGeneratedLogo), and the identity fork is the "
    "operator's (visual-system 'Not made: the identity fork')",
    "screenshot": "a screenshot is captured from the running product through the UI Bridge, "
    "never generated (D6; a current binding must be `captured`)",
}

DEFAULT_ROUTE: dict[str, Route] = {
    "avatar_fallback": "lucide",
    "empty_state": "traced",
    "onboarding": "traced",
    "feature": "diffusion",
    "hero": "diffusion",
    "background": "diffusion",
    "scene": "diffusion",
    "og_card": "diffusion",
}

# The image-facade recipe (qontinui docker/imagegen/recipes) a route asks for,
# by role. ``traced`` asks for a RASTER recipe and traces locally: the facade's
# own ``empty-state`` recipe vectorises on the facade, and the facade's SVG
# result shape is Phase 10's to define.
DEFAULT_RECIPE: dict[Route, dict[str, str]] = {
    "diffusion": {"hero": "hero"},
    "traced": {},
}
FALLBACK_RECIPE: dict[Route, str] = {"diffusion": "illustration", "traced": "cut-out"}

# The widest CSS-pixel box a role is laid out in, before the device pixel
# ratio: the master derivative is that times the spec's largest DPR, never
# wider than its widest breakpoint at that DPR, never past MAX_WIDTH_PX. A
# target overrides it per slot (``width``) when a layout says otherwise.
ROLE_CSS_WIDTH: dict[str, int] = {
    "hero": 1440,
    "background": 1920,
    "scene": 1200,
    "feature": 720,
    "onboarding": 640,
    "empty_state": 480,
    "avatar_fallback": 128,
}
OG_CARD_WIDTH = 1200  # an og:image is fetched by crawlers at its own pixel size
MAX_WIDTH_PX = 4096

LUCIDE_NAME = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
# A spec slot id (qontinui-schemas SLUG_PATTERN).
SLOT_ID = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"


class SlotOverride(BaseModel):
    """A target's per-slot settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route: Route | None = None
    # The lucide icon (kebab-case, as lucide names it) a ``lucide`` slot uses.
    icon: str | None = Field(default=None, pattern=LUCIDE_NAME)
    # The facade recipe id to ask for, overriding the route's default.
    recipe: str | None = Field(default=None, pattern=ID_PATTERN)
    # The master derivative's width in pixels.
    width: int | None = Field(default=None, gt=0, le=MAX_WIDTH_PX)
    # A prompt fragment (a "look", like style/*.md) for this slot only.
    look: Path | None = None


class CompileTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1)
    # GitHub owner/name: the dial is read for it, and the image store keys its
    # slot candidates by it.
    repo: str = Field(pattern=REPO_PATTERN)
    commercial: bool
    ai_label: str = DEFAULT_AI_LABEL
    slots_dir: Path = Path("frontend/image-slots")
    publish_dir: Path = Path("frontend/public/image-slots")
    providers: list[str] = Field(min_length=1)
    # N: candidates per slot (best-of-N, Phase 20; until then the lowest index).
    candidates: int = Field(default=4, ge=1, le=8)
    look: Path | None = None
    slots: dict[str, SlotOverride] = Field(default_factory=dict)
    # The D7 preview (all four or none, as on any site): D7 is required with no
    # waiver, so a target without one is never written to.
    ui_bridge_url: str | None = None
    preview_url: str | None = None
    preview_dir: Path | None = None
    preview_manifest_url: str | None = None

    @field_validator("slots_dir", "publish_dir")
    @classmethod
    def _inside_checkout(cls, v: Path) -> Path:
        pure = PurePosixPath(v.as_posix())
        if pure.is_absolute() or ".." in pure.parts or str(pure) in ("", "."):
            raise ValueError(f"'{v}' must be a relative path inside the checkout (no '..')")
        return Path(pure.as_posix())

    @field_validator("slots")
    @classmethod
    def _slot_ids(cls, v: dict[str, SlotOverride]) -> dict[str, SlotOverride]:
        for slot_id, o in v.items():
            if not re.fullmatch(SLOT_ID, slot_id):
                raise ValueError(f"slot '{slot_id}' is not a spec slot id")
            if o.route == "lucide" and o.recipe is not None:
                raise ValueError(
                    f"slot '{slot_id}': a lucide slot is selected, so it has no recipe"
                )
        return v

    @field_validator("providers")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("providers names a provider twice")
        return v

    @model_validator(mode="after")
    def _paths_apart(self) -> CompileTarget:
        a, b = PurePosixPath(self.slots_dir.as_posix()), PurePosixPath(self.publish_dir.as_posix())
        if a == b or a.is_relative_to(b) or b.is_relative_to(a):
            raise ValueError("slots_dir and publish_dir must not overlap")
        return self

    # ---- routing ----------------------------------------------------------

    def override(self, slot_id: str) -> SlotOverride:
        return self.slots.get(slot_id) or SlotOverride()

    def recipe_for(self, spec: LoadedSpec, route: Route) -> str | None:
        o = self.override(spec.slot_id)
        if o.recipe is not None:
            return o.recipe
        if route not in ASSET_ROUTES:
            return None
        role = spec.spec.role.value
        return DEFAULT_RECIPE[route].get(role, FALLBACK_RECIPE[route])

    def look_for(self, slot_id: str) -> Path | None:
        o = self.override(slot_id)
        return o.look if o.look is not None else self.look

    # ---- the publication target -------------------------------------------

    def publish_path(self, checkout: Path) -> Path:
        return checkout / self.publish_dir

    def slot_for(self, spec: LoadedSpec, route: Route) -> Slot:
        """The studio site slot for an asset-route spec: its pixel size, its
        formats, its focal point and whether it is an ``<img>`` at all."""
        role = spec.spec.role.value
        w_ratio, h_ratio = parse_aspect(spec.spec.aspect.preferred)
        width = self.override(spec.slot_id).width or master_width(spec)
        height = max(1, round(width * h_ratio / w_ratio))
        r = spec.spec.focal.region
        formats: list[ExportFormat]
        if route == "traced":
            # The trace, and a PNG of the same crop for a consumer that renders
            # only raster slots (qontinui-web's <SlotImage> reads png/jpg/webp/avif).
            formats = ["svg", "png"]
        elif role == "og_card":
            formats = ["png"]
        else:
            # PNG is the fallback every consumer decodes; studio's format key for
            # JPEG ("jpeg") is not one qontinui-web's slot reader accepts.
            formats = ["avif", "webp", "png"]
        category: SlotCategory = "illustration" if route == "traced" else "decorative"
        return Slot(
            width=width,
            height=height,
            category=category,
            formats=formats,
            # An og:image is a <meta> tag, never an <img>: D7 fetches it.
            rendered=role != "og_card",
            focal_x=min(1.0, max(0.0, r.x + r.width / 2)),
            focal_y=min(1.0, max(0.0, r.y + r.height / 2)),
        )

    def site(self, checkout: Path, slots: dict[str, Slot]) -> Site:
        """This target as a Site whose ``publish_dir`` is inside ``checkout``.
        Raises :class:`ConfigError` when it does not validate (e.g. a half
        preview config, or a preview dir inside the checkout's publish dir)."""
        if not slots:
            raise ConfigError(f"target '{self.id}': no slot to publish")
        try:
            return Site.model_validate(
                {
                    "id": self.id,
                    "name": self.name,
                    "kind": "product",
                    "commercial": self.commercial,
                    "publish_dir": self.publish_path(checkout),
                    "repo": self.repo,
                    "ai_label": self.ai_label,
                    "checkout": True,
                    "ui_bridge_url": self.ui_bridge_url,
                    "preview_url": self.preview_url,
                    "preview_dir": self.preview_dir,
                    "preview_manifest_url": self.preview_manifest_url,
                    "slots": slots,
                }
            )
        except ValueError as e:
            raise ConfigError(f"target '{self.id}': {e}") from e


def master_width(spec: LoadedSpec) -> int:
    role = spec.spec.role.value
    if role == "og_card":
        return OG_CARD_WIDTH
    dpr = max(d.root for d in spec.spec.contexts.dpr)
    widest = max(b.root for b in spec.spec.contexts.breakpoints)
    css = min(ROLE_CSS_WIDTH.get(role, 1200), widest)
    return max(1, min(MAX_WIDTH_PX, round(css * dpr)))


def route_for(target: CompileTarget, spec: LoadedSpec) -> tuple[Route | None, str]:
    """The route for ``spec`` and where it came from; ``(None, why)`` for a
    role studio never compiles."""
    role = spec.spec.role.value
    if role in NOT_COMPILED:
        return None, NOT_COMPILED[role]
    declared = target.override(spec.slot_id).route
    if declared is not None:
        return declared, f"declared for this slot in target '{target.id}'"
    return DEFAULT_ROUTE[role], f"the default for role '{role}'"


def load_target(path: Path, root: Path) -> CompileTarget:
    """Load a target. ``look`` paths resolve relative to the target file and
    must stay inside the studio ``root`` (a target cannot pull an arbitrary
    file from the machine into a provider request); ``preview_dir`` resolves
    relative to the file too."""
    root = root.resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
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

    if raw.get("look") is not None:
        raw["look"] = confined(raw["look"])
    for slot in (raw.get("slots") or {}).values():
        if isinstance(slot, dict) and slot.get("look") is not None:
            slot["look"] = confined(slot["look"])
    if isinstance(raw.get("preview_dir"), str) and not Path(raw["preview_dir"]).is_absolute():
        raw["preview_dir"] = str((base / raw["preview_dir"]).resolve())
    try:
        return CompileTarget.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_targets(directory: Path, root: Path) -> dict[str, CompileTarget]:
    targets: dict[str, CompileTarget] = {}
    if not directory.is_dir():
        return targets
    for path in sorted(directory.glob("*.yaml")):
        t = load_target(path, root)
        if t.id in targets:
            raise ConfigError(f"duplicate target id '{t.id}' ({path})")
        targets[t.id] = t
    return targets
