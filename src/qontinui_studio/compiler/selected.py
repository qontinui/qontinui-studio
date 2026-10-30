"""The two routes that generate nothing (plan 2026-10-13 Phase 19 step 2).

* ``lucide`` — icons are selected from lucide, never generated
  (``domain_spec/visual-system`` "Imagery & iconography"). Studio writes no
  file: the slot renders a ``lucide-react`` component. It checks the icon
  exists in the checkout's installed ``lucide-react`` and names its component.
* ``svg`` — simple illustrations are agent-written inline SVG in
  ``currentColor`` and token variables. Both only work INLINE (an ``<img>``
  inherits neither the page's text colour nor its custom properties), so the
  SVG is source the calling session writes beside the spec, as
  ``<slots_dir>/<slot>.svg``; studio writes only derivatives and the manifest
  (plan Phase 21 step 1). Studio hands the session the brief and the contract,
  and validates what was written: well-formed; only drawing elements; every
  colour ``none``, ``currentColor`` or ``var(--qontinui-*)``; nothing that runs
  or fetches; a ``viewBox`` in one of the spec's aspect ratios; the spec's
  content hash on the root as ``data-slot-spec-sha256`` (so a spec edit makes
  the drawing stale, not silently current); and the IPTC AI-generated marker
  in its ``<metadata>`` (a model wrote it).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET
from xml.parsers import expat

from qontinui_studio import marker
from qontinui_studio.compiler.specs import LoadedSpec, parse_aspect

SVG = f"{{{marker.SVG_NS}}}"
DRAWING_ELEMENTS = frozenset(
    {"svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon"}
)
DESCRIPTIVE_ELEMENTS = frozenset({"title", "desc", "metadata"})
TEXT_ELEMENTS = frozenset({"text", "tspan"})
ATTRIBUTES = frozenset(
    {
        "xmlns",
        "viewBox",
        "width",
        "height",
        "fill",
        "fill-opacity",
        "fill-rule",
        "clip-rule",
        "stroke",
        "stroke-opacity",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "stroke-dasharray",
        "opacity",
        "transform",
        "d",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "points",
        "role",
        "aria-hidden",
        "aria-label",
        "focusable",
        "data-slot-spec-sha256",
    }
)
TEXT_ATTRIBUTES = frozenset({"font-size", "font-weight", "text-anchor", "dominant-baseline"})
COLOUR_ATTRIBUTES = frozenset({"fill", "stroke", "color", "stop-color"})
TOKEN_COLOUR = re.compile(r"^var\(--qontinui-[a-z0-9-]+\)$")
HASH_ATTRIBUTE = "data-slot-spec-sha256"
ASPECT_TOLERANCE = 0.01

AuthoredStatus = Literal["awaiting_authoring", "authored", "authored_stale", "authored_invalid"]


@dataclass(frozen=True)
class AuthoredSvg:
    status: AuthoredStatus
    path: Path
    problems: list[str] = field(default_factory=list)


def authored_path(slots_dir: Path, spec: LoadedSpec) -> Path:
    return slots_dir / f"{spec.slot_id}.svg"


def _local(tag: str) -> str | None:
    """An element's local name when it is in the SVG namespace, else None."""
    return tag[len(SVG) :] if tag.startswith(SVG) else None


_X = "{adobe:ns:meta/}"
_RDF = f"{{{marker.RDF_NS}}}"
_DST = f"{{{marker.IPTC_EXT_NS}}}DigitalSourceType"
# The RDF an authored SVG's marker packet may hold, and nothing else: one
# rdf:RDF of rdf:Description nodes that state DigitalSourceType (as an
# attribute, or as an element carrying it as text or rdf:resource).
_PACKET_ATTRIBUTES = {
    f"{_RDF}Description": {f"{_RDF}about", _DST},
    _DST: {f"{_RDF}resource"},
}


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


def _metadata_problems(meta: ET.Element) -> list[str]:
    """The root's ``<metadata>`` holds exactly one ``x:xmpmeta`` packet whose
    RDF states the AI-generated marker and nothing else: no other element, no
    attribute outside the allowlist, no text."""
    problems: list[str] = []
    if meta.attrib or not _blank(meta.text) or len(meta) != 1 or meta[0].tag != f"{_X}xmpmeta":
        return ["<metadata> must hold exactly one x:xmpmeta packet and nothing else"]
    xmp = meta[0]
    if xmp.attrib or not _blank(xmp.text) or not _blank(xmp.tail):
        problems.append("x:xmpmeta takes no attribute or text")
    if len(xmp) != 1 or xmp[0].tag != f"{_RDF}RDF":
        return [*problems, "x:xmpmeta must hold exactly one rdf:RDF"]
    rdf = xmp[0]
    if rdf.attrib or not _blank(rdf.text) or not _blank(rdf.tail):
        problems.append("rdf:RDF takes no attribute or text")
    for desc in rdf:
        if desc.tag != f"{_RDF}Description" or not _blank(desc.tail):
            problems.append(f"rdf:RDF may hold rdf:Description only, not <{desc.tag}>")
            continue
        for key, value in desc.attrib.items():
            if key not in _PACKET_ATTRIBUTES[desc.tag]:
                problems.append(f"rdf:Description attribute '{key}' is not allowed")
            elif key == f"{_RDF}about" and value != "":
                problems.append("rdf:Description rdf:about must be empty")
            elif key == _DST and value != marker.DIGITAL_SOURCE_TYPE:
                problems.append("DigitalSourceType is not trainedAlgorithmicMedia")
        if not _blank(desc.text):
            problems.append("rdf:Description takes no text")
        for prop in desc:
            if prop.tag != _DST or len(prop) or not _blank(prop.tail):
                problems.append(
                    f"rdf:Description may state DigitalSourceType only, not <{prop.tag}>"
                )
                continue
            for key in prop.attrib:
                if key not in _PACKET_ATTRIBUTES[_DST]:
                    problems.append(f"DigitalSourceType attribute '{key}' is not allowed")
            if prop.get(f"{_RDF}resource") is not None and not _blank(prop.text):
                problems.append("DigitalSourceType carries both rdf:resource and text")
            value = (prop.get(f"{_RDF}resource") or prop.text or "").strip()
            if value != marker.DIGITAL_SOURCE_TYPE:
                problems.append("DigitalSourceType is not trainedAlgorithmicMedia")
    return problems


def _processing_instructions(data: bytes) -> list[str]:
    """Every processing instruction in the document (``<?xml-stylesheet?>``
    among them): an inline drawing carries none. The XML declaration is not
    one."""
    found: list[str] = []
    parser = expat.ParserCreate()
    parser.ProcessingInstructionHandler = lambda target, _data: found.append(target)
    try:
        parser.Parse(data, True)
    except expat.ExpatError:
        return ["not well-formed XML"]
    return [f"processing instruction <?{t}?> is not allowed" for t in found]


def _aspects(spec: LoadedSpec) -> list[float]:
    ratios = [spec.spec.aspect.preferred, *(a.root for a in spec.spec.aspect.allowed)]
    return [w / h for w, h in (parse_aspect(r) for r in ratios)]


def check_authored_svg(slots_dir: Path, spec: LoadedSpec) -> AuthoredSvg:
    path = authored_path(slots_dir, spec)
    if not path.is_file():
        return AuthoredSvg("awaiting_authoring", path)
    data = path.read_bytes()
    root = marker.svg_root(data)
    if root is None:
        return AuthoredSvg(
            "authored_invalid", path, ["not a well-formed SVG document (or it declares a DTD)"]
        )
    problems: list[str] = []
    text_ok = spec.spec.text_in_image.root.kind != "forbidden"
    allowed = DRAWING_ELEMENTS | DESCRIPTIVE_ELEMENTS | (TEXT_ELEMENTS if text_ok else frozenset())
    attrs = ATTRIBUTES | (TEXT_ATTRIBUTES if text_ok else frozenset())

    def walk(el: ET.Element) -> None:
        name = _local(el.tag)
        if name == "metadata":
            if el is not root.find(f"{SVG}metadata") or el not in list(root):
                problems.append("<metadata> may appear once, as a direct child of the root")
            else:
                problems.extend(_metadata_problems(el))
            return
        if name is None or name not in allowed:
            problems.append(f"element <{el.tag}> is not allowed")
            return
        if name in ("title", "desc") and len(el):
            problems.append(f"<{name}> holds text only")
        for key, value in el.attrib.items():
            if key not in attrs:
                problems.append(f"<{name}> attribute '{key}' is not allowed")
            elif key in COLOUR_ATTRIBUTES and not (
                value in ("none", "currentColor") or TOKEN_COLOUR.fullmatch(value)
            ):
                problems.append(
                    f"<{name} {key}='{value}'>: a colour is none, currentColor or "
                    "var(--qontinui-*), never a literal"
                )
        for child in el:
            walk(child)

    walk(root)
    metas = [el for el in root.iter() if el.tag == f"{SVG}metadata"]
    if len(metas) != 1:
        problems.append(
            f"the document holds {len(metas)} <metadata> elements; it needs exactly one"
        )
    problems.extend(_processing_instructions(data))
    if not text_ok and any(_local(el.tag) in TEXT_ELEMENTS for el in root.iter()):
        problems.append("the slot forbids text in the image")
    box = (root.get("viewBox") or "").replace(",", " ").split()
    try:
        x, y, w, h = (float(v) for v in box)
        if w <= 0 or h <= 0:
            raise ValueError
        if not any(abs(w / h - a) / a <= ASPECT_TOLERANCE for a in _aspects(spec)):
            problems.append(
                f"viewBox ratio {w / h:.4f} is none of the slot's aspect ratios "
                f"({spec.spec.aspect.preferred} or an allowed one)"
            )
    except ValueError:
        problems.append("the root needs a viewBox of four numbers with a positive size")
    if not marker.bytes_are_marked(data):
        problems.append(
            "no IPTC AI-generated marker: put exactly one x:xmpmeta packet declaring "
            "trainedAlgorithmicMedia in the root's <metadata>"
        )
    if problems:
        return AuthoredSvg("authored_invalid", path, problems)
    if root.get(HASH_ATTRIBUTE) != spec.sha256:
        return AuthoredSvg(
            "authored_stale",
            path,
            [
                f"{HASH_ATTRIBUTE} is {root.get(HASH_ATTRIBUTE)!r}; the spec now hashes to "
                f"{spec.sha256} (redraw it for the current spec)"
            ],
        )
    return AuthoredSvg("authored", path)


def authoring_brief(spec: LoadedSpec, prompt: str) -> dict[str, object]:
    """What the calling session needs to write the SVG, as data."""
    w, h = parse_aspect(spec.spec.aspect.preferred)
    text_ok = spec.spec.text_in_image.root.kind != "forbidden"
    return {
        "write_to": f"<slots_dir>/{spec.slot_id}.svg",
        "brief": prompt,
        "root": {
            "viewBox": f"0 0 {w * 100} {h * 100}",
            HASH_ATTRIBUTE: spec.sha256,
        },
        "metadata": f"<metadata>{_xmpmeta()}</metadata>",
        "elements": sorted(
            DRAWING_ELEMENTS | DESCRIPTIVE_ELEMENTS | (TEXT_ELEMENTS if text_ok else frozenset())
        ),
        "colours": "none | currentColor | var(--qontinui-<token>) only; no style attribute",
    }


def _xmpmeta() -> str:
    packet = marker.XMP_PACKET.decode("utf-8")
    start = packet.index("<x:xmpmeta")
    end = packet.index("</x:xmpmeta>") + len("</x:xmpmeta>")
    return packet[start:end]


# ---- lucide ------------------------------------------------------------------


LucideStatus = Literal["selected", "icon_unknown", "unverified"]


@dataclass(frozen=True)
class LucideIcon:
    status: LucideStatus
    icon: str
    component: str
    detail: str


def component_name(icon: str) -> str:
    """lucide-react's export for a kebab-case icon (``circle-user`` ->
    ``CircleUser``)."""
    return "".join(part[:1].upper() + part[1:] for part in icon.split("-"))


def check_lucide(icon: str, checkout: Path, slots_dir: Path) -> LucideIcon:
    """Whether ``icon`` exists in the ``lucide-react`` installed for the
    frontend the slots belong to: the nearest ``node_modules`` from the slots
    directory up to the checkout. Not installed, or an unrecognised package
    layout, is UNKNOWN (``unverified``), never "exists"."""
    component = component_name(icon)
    here = slots_dir.resolve()
    top = checkout.resolve()
    package: Path | None = None
    while True:
        candidate = here / "node_modules" / "lucide-react"
        if (candidate / "package.json").is_file():
            package = candidate
            break
        if here == top or here.parent == here:
            break
        here = here.parent
    if package is None:
        return LucideIcon(
            "unverified", icon, component, "lucide-react is not installed under this checkout"
        )
    try:
        version = json.loads((package / "package.json").read_text(encoding="utf-8")).get(
            "version", "?"
        )
    except (OSError, ValueError):
        version = "?"
    icons = package / "dist" / "esm" / "icons"
    if not icons.is_dir():
        return LucideIcon(
            "unverified",
            icon,
            component,
            f"lucide-react {version} has no dist/esm/icons directory (an unknown layout)",
        )
    if (icons / f"{icon}.js").is_file():
        return LucideIcon("selected", icon, component, f"lucide-react {version}")
    return LucideIcon("icon_unknown", icon, component, f"lucide-react {version} has no '{icon}'")
