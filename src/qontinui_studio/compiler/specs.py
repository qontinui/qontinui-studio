"""Image-slot specs: ``<repo>/frontend/image-slots/*.yaml`` (plan 2026-10-13
Phase 14), read through the generated ``ImageSlotSpec`` model from
qontinui-schemas (PD3: one drift-gated contract for Python, TS and Rust).

A spec is valid iff it validates STRICTLY against the generated model AND passes
the cross-field rules the JSON Schema cannot express. The Rust type states both
halves (``ImageSlotSpec::validate``, qontinui-schemas ``rust/src/image_slot.rs``)
and says a validator in another language must mirror the cross-field rules by
hand; :func:`cross_field_problems` is that mirror.

YAML is read with PyYAML, a YAML 1.1 loader: an unquoted ``yes``/``no``/``on``/
``off`` becomes a boolean and ``4:3`` the integer 243. Strict validation of the
loaded value's JSON form refuses every such coercion where the contract wants a
string, rather than accepting a silently different spec.

The content hash (:func:`content_sha256`) is the Rust one: sha256 of the spec's
RFC 8785 canonical JSON with ``provenance`` left out — the key every binding,
manifest entry and staleness decision is made on.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from qontinui_schemas.generated.per_type.image_slot_spec import (
    ImageSlotProvenance,
    ImageSlotSpec,
    ImageSlotThresholds2,
)

from qontinui_studio.compiler.jcs import CanonicalizationError, canonical_sha256
from qontinui_studio.errors import ConfigError

SPEC_SUFFIX = ".yaml"
_ASPECT = re.compile(r"^([1-9][0-9]{0,3})/([1-9][0-9]{0,3})$")


def brief_slug(slot_id: str) -> str:
    """A slot id in the studio's dot-free id vocabulary (brief and asset ids):
    ``sessions.empty-state`` -> ``sessions--empty-state``. A spec slot id never
    holds two separators in a row, so '--' never occurs in one and the mapping
    is injective. The slot itself — its site slot, its file stem and its
    manifest key — keeps the spec's own id."""
    return slot_id.replace(".", "--")


def parse_aspect(value: str) -> tuple[int, int]:
    m = _ASPECT.fullmatch(value)
    if m is None:
        raise ValueError(f"aspect ratio '{value}' is not W/H")
    return int(m.group(1)), int(m.group(2))


def _reduce(w: int, h: int) -> tuple[int, int]:
    return (w // math.gcd(w, h), h // math.gcd(w, h))


# Rust's `str::trim` removes exactly the Unicode White_Space characters, which
# is not Python's `str.isspace` (that also counts U+001C..U+001F).
_RUST_WHITE_SPACE = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def rust_trim(text: str) -> str:
    """``str::trim`` as Rust defines it."""
    return text.strip(_RUST_WHITE_SPACE)


def _content_key(entry: str) -> str:
    # content_key in image_slot.rs: entry.trim().to_lowercase()
    return rust_trim(entry).lower()


def _rect_problem(x: float, y: float, w: float, h: float) -> bool:
    return w <= 0.0 or h <= 0.0 or x + w > 1.0 or y + h > 1.0


def cross_field_problems(spec: ImageSlotSpec, current_hash: str | None) -> list[str]:
    """The rules ``ImageSlotSpec::validate`` checks that the schema cannot
    (``ImageSlotProblem::is_schema_rule`` false). ``current_hash`` is the
    spec's content hash, used to decide whether an inline binding is current."""
    problems: list[str] = []
    ctx = spec.contexts
    bps = [b.root for b in ctx.breakpoints]
    if any(a >= b for a, b in zip(bps, bps[1:], strict=False)):
        problems.append("contexts.breakpoints: not strictly ascending")
    dprs = [d.root for d in ctx.dpr]
    if any(a >= b for a, b in zip(dprs, dprs[1:], strict=False)):
        problems.append("contexts.dpr: not strictly ascending")
    for i, theme in enumerate(ctx.themes):
        if theme in ctx.themes[:i]:
            problems.append(f"contexts.themes[{i}]: duplicate")
    seen = [_reduce(*parse_aspect(spec.aspect.preferred))]
    for i, ratio in enumerate(spec.aspect.allowed):
        value = _reduce(*parse_aspect(ratio.root))
        if value in seen:
            problems.append(f"aspect.allowed[{i}]: duplicate (equal in lowest terms)")
        else:
            seen.append(value)
    for name, entries in (("must", spec.content.must), ("must_not", spec.content.must_not)):
        keys = [_content_key(e.root) for e in entries]
        for i, key in enumerate(keys):
            if key in keys[:i]:
                problems.append(f"content.{name}[{i}]: duplicate")
    must = {_content_key(e.root) for e in spec.content.must}
    for i, entry in enumerate(spec.content.must_not):
        if _content_key(entry.root) in must:
            problems.append(f"content.must_not[{i}]: also required (contradictory)")
    r = spec.focal.region
    if _rect_problem(r.x, r.y, r.width, r.height):
        problems.append("focal.region: no area, or past the frame's edge")
    areas = spec.safe_areas.root
    for i, region in enumerate(getattr(areas, "regions", None) or []):
        if _rect_problem(region.x, region.y, region.width, region.height):
            problems.append(f"safe_areas.regions[{i}]: no area, or past the box's edge")
    th = spec.thresholds.root
    if isinstance(th, ImageSlotThresholds2):
        values = th.model_dump(exclude={"kind"})
        if all(v is None for v in values.values()):
            problems.append("thresholds: overrides names no threshold")
        lo, hi = th.min_effective_resolution, th.max_effective_resolution
        if lo is not None and hi is not None and lo > hi:
            problems.append("thresholds: min_effective_resolution above max_effective_resolution")
    if spec.provenance is not None:
        problems += binding_problems(spec, spec.provenance, current_hash)
    return problems


def binding_problems(
    spec: ImageSlotSpec, binding: ImageSlotProvenance, current_hash: str | None
) -> list[str]:
    """A binding's own consistency, and — when it is current — its obligations
    toward ``spec`` (``ImageSlotProvenance::check`` and
    ``check_current_binding``)."""
    problems: list[str] = []
    model_made = binding.source.value in ("generated", "edited")
    if model_made:
        if not binding.ai_generated:
            problems.append("provenance.ai_generated: a model-made asset must be marked")
        if binding.recipe_sha256 is None:
            problems.append("provenance.recipe_sha256: missing for a model-made asset")
        if binding.imagery_sha256 is None:
            problems.append("provenance.imagery_sha256: missing for a model-made asset")
    if current_hash is not None and binding.spec_sha256 == current_hash:
        if spec.alt.root.kind == "required" and binding.alt_text is None:
            problems.append("provenance.alt_text: an alt: required slot owes one")
        if spec.style.exemplars is not None and binding.exemplars_sha256 is None:
            problems.append("provenance.exemplars_sha256: the slot names an exemplar set")
        if spec.style.lora is not None and model_made and binding.lora_sha256 is None:
            problems.append("provenance.lora_sha256: the slot names a LoRA")
        if spec.role.value == "logo" and binding.ai_generated:
            problems.append("provenance.ai_generated: a logo is never AI-generated")
        if spec.role.value == "screenshot" and binding.source.value != "captured":
            problems.append("provenance.source: a screenshot is captured")
    return problems


def content_sha256(spec: ImageSlotSpec) -> str:
    """The spec's content address: sha256 of its RFC 8785 canonical JSON with
    ``provenance`` left out (qontinui-schemas ``ImageSlotSpec::content_sha256``).
    Raises :class:`ConfigError` for a number RFC 8785 cannot carry."""
    data = spec.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"provenance"})
    try:
        return canonical_sha256(data)
    except CanonicalizationError as e:
        raise ConfigError(f"slot '{spec.slot}': no content hash: {e}") from e


@dataclass(frozen=True)
class LoadedSpec:
    spec: ImageSlotSpec
    path: Path
    sha256: str  # the content hash

    @property
    def slot_id(self) -> str:
        return self.spec.slot

    @property
    def site_slot(self) -> str:
        """The studio site slot: the spec's own id (``runs.active.idle``), the
        stem of every published file and the manifest key a consumer reads."""
        return self.spec.slot


def _strict(raw: Any, path: Path) -> ImageSlotSpec:
    try:
        text = json.dumps(raw, allow_nan=True)
    except (TypeError, ValueError) as e:
        # A YAML date, a set or a binary scalar has no JSON form at all.
        raise ConfigError(f"{path}: not representable as JSON ({e})") from e
    try:
        return ImageSlotSpec.model_validate_json(text, strict=True)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise ConfigError(f"{path}: not a valid ImageSlotSpec: {problems}") from e


class _StrictLoader(yaml.SafeLoader):
    """PyYAML's safe loader, refusing what serde_yaml would not read the same
    way: a key given twice (PyYAML keeps the last silently) and a ``<<`` merge
    key (YAML 1.1 only; YAML 1.2 has none)."""


def _strict_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen: list[object] = []
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise yaml.constructor.ConstructorError(
                None, None, "a '<<' merge key is refused", key_node.start_mark
            )
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"the key {key!r} is given twice", key_node.start_mark
            )
        seen.append(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping)


def read_spec(path: Path) -> LoadedSpec:
    """Read and validate one spec file — the contract alone, whatever the file
    is called. :func:`load_spec` adds the naming rule."""
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)  # noqa: S506
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")
    spec = _strict(raw, path)
    digest = content_sha256(spec)
    problems = cross_field_problems(spec, digest)
    if problems:
        raise ConfigError(f"{path}: invalid spec: " + "; ".join(problems))
    return LoadedSpec(spec=spec, path=path, sha256=digest)


def load_spec(path: Path) -> LoadedSpec:
    """One spec of a slots directory: valid, and named after its slot, so one
    slot has exactly one file."""
    loaded = read_spec(path)
    if path.stem != loaded.slot_id:
        raise ConfigError(
            f"{path}: the file is named '{path.stem}' but declares slot '{loaded.slot_id}'; a "
            "spec file is named after its slot"
        )
    return loaded


def load_specs(directory: Path) -> list[LoadedSpec]:
    """Every ``*.yaml`` spec under ``directory`` (not recursive), sorted by slot.
    A missing directory is a configuration error, never an empty slot set."""
    if not directory.is_dir():
        raise ConfigError(f"{directory}: no image-slots directory")
    specs = [load_spec(p) for p in sorted(directory.glob(f"*{SPEC_SUFFIX}"))]
    seen: dict[str, Path] = {}
    for s in specs:
        if s.site_slot in seen:
            raise ConfigError(
                f"slot '{s.slot_id}' is declared twice ({seen[s.site_slot]}, {s.path})"
            )
        seen[s.site_slot] = s.path
    return sorted(specs, key=lambda s: s.slot_id)
