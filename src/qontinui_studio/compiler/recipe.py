"""What a compiled slot is generated FROM: the prompt a spec becomes, and the
compile recipe hash a binding records as ``recipe_sha256``.

The prompt is derived from the spec alone (its job, required content, text
policy, focal region, safe areas and themes), so the spec's content hash
already covers it; ``must_not`` and a ``forbidden`` text policy become the
negative prompt, which a wire with a negative-prompt field carries as such
(plan Phase 11 step 2).

The compile recipe identifies everything ELSE that decides the pixels: the
route, the provider row (adapter and model), the facade recipe id and — for
the fleet's own image facade — that recipe's own hash as the facade reports
it, the look (prompt fragment) file's bytes, the prompt builder's version and
the derivatives cut (size, focal point, formats, trace parameters). Any change
to one of them is a recipe change, and a binding made under the old one is
stale.
"""

from __future__ import annotations

from typing import Any

from qontinui_studio.compiler.jcs import canonical_sha256
from qontinui_studio.compiler.specs import LoadedSpec
from qontinui_studio.compiler.target import Route
from qontinui_studio.postprocess import TRACE_PARAMETERS
from qontinui_studio.sites import Slot

PROMPT_BUILDER = "qontinui-studio/compile-prompt/v1"
RECIPE_SCHEMA = "qontinui-studio/compile-recipe/v1"
# The adapter kind whose results carry the facade's own recipe hash.
FACADE_ADAPTER = "openai_compatible_images"


def _third(value: float, names: tuple[str, str, str]) -> str:
    return names[0] if value < 1 / 3 else names[2] if value > 2 / 3 else names[1]


def _place(x: float, y: float, w: float, h: float) -> str:
    cx, cy = x + w / 2, y + h / 2
    return (
        f"in the {_third(cy, ('upper', 'middle', 'lower'))} "
        f"{_third(cx, ('left', 'centre', 'right'))}"
    )


def prompt_for(spec: LoadedSpec, route: Route) -> tuple[str, str | None]:
    """(prompt, negative prompt or None) for ``spec`` on ``route``."""
    s = spec.spec
    parts = [s.job.strip()]
    if s.content.must:
        parts.append("Depict: " + "; ".join(m.root.strip() for m in s.content.must) + ".")
    text = s.text_in_image.root
    if text.kind == "expected":
        parts.append(f'The image carries exactly this text, in {text.locale}: "{text.text}".')
    elif text.kind == "forbidden":
        parts.append("The image carries no text.")
    r = s.focal.region
    parts.append(
        f"Place the subject {_place(r.x, r.y, r.width, r.height)} of the frame"
        + (
            ", wholly inside that area, so every crop keeps it."
            if s.focal.must_survive_crop
            else "."
        )
    )
    areas = getattr(s.safe_areas.root, "regions", None)
    if areas:
        calm = "; ".join(_place(a.x, a.y, a.width, a.height) for a in areas)
        parts.append(f"Keep the area {calm} calm and free of detail: page content is laid over it.")
    themes = " and ".join(t.value for t in s.contexts.themes)
    parts.append(f"It is shown on a {themes} page.")
    if route == "traced":
        parts.append(
            "Flat illustration with a few solid colours, no gradients and no texture, "
            "on a plain background."
        )
    negative = [m.root.strip() for m in s.content.must_not]
    if text.kind == "forbidden":
        negative += ["text", "letters", "words"]
    return " ".join(parts), (", ".join(negative) if negative else None)


def compile_recipe(
    *,
    route: Route,
    provider_id: str,
    adapter: str,
    model: str,
    recipe_id: str | None,
    engine_recipe_sha256: str | None,
    look_sha256: str | None,
    slot: Slot,
) -> dict[str, Any]:
    """The compile recipe document (its canonical hash is the binding's
    ``recipe_sha256``)."""
    derivatives: dict[str, Any] = {
        "width": slot.width,
        "height": slot.height,
        "formats": list(slot.formats),
        "focal": [slot.focal_x, slot.focal_y],
        "remove_background": slot.remove_background,
    }
    if "svg" in slot.formats:
        derivatives["trace"] = dict(TRACE_PARAMETERS)
    return {
        "schema": RECIPE_SCHEMA,
        "route": route,
        "provider": provider_id,
        "adapter": adapter,
        "model": model,
        "recipe": recipe_id,
        "engine_recipe_sha256": engine_recipe_sha256,
        "look_sha256": look_sha256,
        "prompt_builder": PROMPT_BUILDER,
        "derivatives": derivatives,
    }


def compile_recipe_sha256(**kwargs: Any) -> str:
    return canonical_sha256(compile_recipe(**kwargs))
