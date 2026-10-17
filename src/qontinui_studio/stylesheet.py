"""The style-sheet page: ``style/guide.yaml`` rendered as one static HTML page.

The guide (plan 2026-10-09-qontinui-marketing-studio-animated-process-
explainers, D4) holds only what is studio-specific: the twin metaphor, the icon
set, the prompt fragments ("looks") and the reference set. Palette, motion and
typography are NAMED, never valued, in the guide: their values come from
``@qontinui/design-tokens``' ``tokens.css``, which the caller points at. A guide
token missing from that file refuses the render; with no file at all every
value renders as unresolved (UNKNOWN), never as a guessed default.

The reference set is a library query (``Library.tagged``), so the page shows
what the library holds right now, approved or not.
"""

from __future__ import annotations

import html
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from qontinui_studio.errors import ConfigError
from qontinui_studio.library import TAG_PATTERN, AssetRecord, Library, is_tag

GUIDE_FORMAT = "qontinui-studio/style-guide"
TOKEN_PATTERN = r"^--qontinui-[a-z0-9-]+$"
ICON_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")

# The icon contract: plain line drawings on a 24x24 grid, coloured by the page
# or scene that draws them (``currentColor``). Anything outside these elements
# and attributes (scripts, styles, links, embedded images, event handlers) is
# refused, so an icon can be inlined into a page or a scene safely.
SVG_NS = "http://www.w3.org/2000/svg"
ICON_ELEMENTS = {"svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon"}
ICON_ATTRIBUTES = {
    "xmlns",
    "viewBox",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-dasharray",
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
    "width",
    "height",
    "points",
    "opacity",
}
ICON_COLOURS = {"none", "currentColor"}


def _check_tag(v: str) -> str:
    if not is_tag(v):
        raise ValueError(f"'{v}' must match {TAG_PATTERN}")
    return v


class TokenRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(pattern=TOKEN_PATTERN)
    role: str = Field(min_length=1)


class Tokens(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package: str = Field(min_length=1)
    min_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    palette: list[TokenRef] = Field(min_length=1)
    motion: list[TokenRef] = Field(min_length=1)
    typography: list[TokenRef] = Field(min_length=1)
    reduced_motion: str = Field(min_length=1)

    def all(self) -> list[TokenRef]:
        return [*self.palette, *self.motion, *self.typography]


class Metaphor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    rules: list[str] = Field(min_length=1)
    do: list[str] = Field(default_factory=list)
    dont: list[str] = Field(default_factory=list)


class Icon(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ICON_PATTERN)
    meaning: str = Field(min_length=1)


class Look(BaseModel):
    """A prompt fragment a brief names as its ``style_guide``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ICON_PATTERN)
    prompt_fragment: str = Field(min_length=1)
    used_for: str = Field(min_length=1)


class References(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: str
    target_approved: int = Field(ge=1)
    briefs: list[str] = Field(default_factory=list)
    note: str | None = None

    @field_validator("tag")
    @classmethod
    def _tag(cls, v: str) -> str:
        return _check_tag(v)


class Guide(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["qontinui-studio/style-guide"]
    version: Literal[1]
    id: str
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    tokens: Tokens
    metaphor: Metaphor
    icons: list[Icon] = Field(min_length=1)
    looks: list[Look] = Field(min_length=1)
    references: References

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_tag(v)

    @field_validator("icons")
    @classmethod
    def _unique_icons(cls, v: list[Icon]) -> list[Icon]:
        ids = [i.id for i in v]
        if len(set(ids)) != len(ids):
            raise ValueError("icons names an icon twice")
        return v


def load_guide(style_dir: Path) -> Guide:
    path = style_dir / "guide.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        guide = Guide.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
    for look in guide.looks:
        if not _inside(style_dir, look.prompt_fragment).is_file():
            raise ConfigError(f"{path}: look '{look.id}' prompt_fragment does not exist")
    return guide


def _inside(base: Path, rel: str) -> Path:
    resolved = (base / rel).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ConfigError(f"'{rel}' resolves outside {base}")
    return resolved


# ---- tokens.css -------------------------------------------------------------------

_DECL = re.compile(r"(--qontinui-[a-z0-9-]+)\s*:\s*([^;{}]+);")


def read_tokens_css(path: Path) -> dict[str, str]:
    """``--qontinui-*`` declarations of a ``tokens.css``. The FIRST declaration of
    each name wins: later ones are theme or ``prefers-reduced-motion``
    overrides of the same token."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read tokens css {path}: {e}") from e
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out: dict[str, str] = {}
    for name, value in _DECL.findall(text):
        out.setdefault(name, " ".join(value.split()))
    if not out:
        raise ConfigError(f"{path} declares no --qontinui-* tokens")
    return out


# ---- icons ------------------------------------------------------------------------


def load_icon(path: Path) -> str:
    """One icon as inline-safe SVG markup, or ConfigError naming what broke the
    icon contract (24x24 viewBox, allowlisted elements/attributes, colour only
    through ``currentColor``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read icon {path}: {e}") from e
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ConfigError(f"icon {path.name}: a DOCTYPE or ENTITY is refused")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ConfigError(f"icon {path.name}: not well-formed XML: {e}") from e
    if root.tag != f"{{{SVG_NS}}}svg":
        raise ConfigError(f"icon {path.name}: the root element must be an SVG <svg>")
    if root.get("viewBox") != "0 0 24 24":
        raise ConfigError(f"icon {path.name}: viewBox must be '0 0 24 24'")
    for el in root.iter():
        ns, _, local = el.tag.rpartition("}")
        if ns.lstrip("{") != SVG_NS or local not in ICON_ELEMENTS:
            raise ConfigError(f"icon {path.name}: element <{local}> is not allowed")
        if (el.text or "").strip() or (el.tail or "").strip():
            raise ConfigError(f"icon {path.name}: text content is not allowed")
        for attr, value in el.attrib.items():
            if attr not in ICON_ATTRIBUTES:
                raise ConfigError(f"icon {path.name}: attribute '{attr}' is not allowed")
            if attr in ("fill", "stroke") and value not in ICON_COLOURS:
                raise ConfigError(
                    f"icon {path.name}: {attr}='{value}' (colour comes from currentColor)"
                )
    ET.register_namespace("", SVG_NS)
    return ET.tostring(root, encoding="unicode")


# ---- the page ---------------------------------------------------------------------


@dataclass(frozen=True)
class StyleSheet:
    page: Path
    tokens_named: int
    tokens_resolved: int
    references_tagged: int
    references_approved: int
    target_approved: int
    fragment_drift: list[str]


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


CSS = """
body { margin:0; padding:16px; background:var(--qontinui-surface-canvas, #111115);
  color:var(--qontinui-text-primary, #fff); font-family:var(--qontinui-font-sans, system-ui, sans-serif);
  font-size:15px; line-height:1.5; }
main { max-width:1100px; margin:0 auto; }
h1 { font-size:28px; margin:0 0 4px; } h2 { font-size:19px; margin:32px 0 8px; }
h3 { font-size:15px; margin:16px 0 6px; color:var(--qontinui-text-secondary, #b4b4b4); }
p, li { color:var(--qontinui-text-secondary, #b4b4b4); }
.muted { color:var(--qontinui-text-muted, #8a8a8a); font-size:13px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:12px; }
.card { background:var(--qontinui-surface-raised, #1e1e22); border:1px solid var(--qontinui-border-default, #3a3a42);
  border-radius:8px; padding:12px; min-width:0; }
.swatch { height:56px; border-radius:6px; border:1px solid var(--qontinui-border-subtle, #2a2a30); margin-bottom:8px; }
.unresolved { background:repeating-linear-gradient(45deg,#2a2a30 0 6px,#1e1e22 6px 12px); }
code { font-family:var(--qontinui-font-mono, monospace); font-size:12px; overflow-wrap:anywhere; }
.value { display:block; margin-top:4px; }
.track { height:10px; border-radius:5px; background:var(--qontinui-surface-active, #2c2c32); position:relative; margin:10px 0; }
.dot { position:absolute; top:-3px; left:0; width:16px; height:16px; border-radius:50%;
  background:var(--qontinui-brand-primary, #4a90d9); animation-name:slide; animation-iteration-count:infinite;
  animation-direction:alternate; }
@keyframes slide { from { left:0; } to { left:calc(100% - 16px); } }
@media (prefers-reduced-motion: reduce) { .dot { animation:none; } }
.icon { color:var(--qontinui-brand-primary, #4a90d9); width:48px; height:48px; }
.icon svg { width:48px; height:48px; }
.refs { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px; }
.refs img { width:100%; height:auto; border-radius:6px; display:block; }
.pass { color:var(--qontinui-done-fg, #4db89d); } .fail { color:var(--qontinui-attention-fg, #e5534b); }
.unknown { color:var(--qontinui-waiting-fg, #e5a853); }
"""


def _css_value_ok(value: str) -> bool:
    return not any(c in value for c in "<>{};\\") and "/*" not in value


def build_style_sheet(
    guide: Guide,
    style_dir: Path,
    out_dir: Path,
    generated_at: datetime,
    *,
    tokens: dict[str, str] | None,
    tokens_source: str | None,
    library: Library | None,
) -> StyleSheet:
    """Write ``out_dir/index.html`` (plus the reference images it shows) and
    return what it found. ``tokens`` is ``read_tokens_css`` of the token
    package's ``tokens.css``; None renders every value unresolved."""
    named = guide.tokens.all()
    if tokens is not None:
        missing = [t.token for t in named if t.token not in tokens]
        if missing:
            raise ConfigError(
                f"the guide names tokens that {tokens_source} does not declare: {missing} "
                f"(the guide needs {guide.tokens.package} >= {guide.tokens.min_version})"
            )
        bad = [k for k, v in tokens.items() if not _css_value_ok(v)]
        if bad:
            raise ConfigError(f"{tokens_source}: token values not safe to embed: {bad}")
    icons = {i.id: load_icon(_inside(style_dir, f"icons/{i.id}.svg")) for i in guide.icons}

    out_dir.mkdir(parents=True, exist_ok=True)
    refs_dir = out_dir / "refs"
    if refs_dir.exists():
        shutil.rmtree(refs_dir)

    def value_of(token: str) -> str | None:
        return None if tokens is None else tokens.get(token)

    root_vars = "".join(f"{k}:{v};" for k, v in sorted((tokens or {}).items()))
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_e(guide.title)}</title>",
        f"<style>:root{{{root_vars}}}{CSS}</style></head><body><main>",
        f"<h1>{_e(guide.title)}</h1>",
        f"<p>{_e(guide.summary)}</p>",
        f"<p class='muted'>Rendered {_e(generated_at.isoformat())} from <code>style/guide.yaml</code>. "
        f"Token values: "
        + (
            f"<code>{_e(tokens_source)}</code>"
            if tokens is not None
            else "<span class='unknown'>UNRESOLVED — no tokens.css given; every value below is "
            "UNKNOWN, not a default</span>"
        )
        + f". Authority: <code>{_e(guide.tokens.package)}</code> &ge; "
        f"{_e(guide.tokens.min_version)}.</p>",
    ]

    def token_cards(refs: list[TokenRef], kind: str) -> None:
        parts.append("<div class='grid'>")
        for ref in refs:
            value = value_of(ref.token)
            parts.append("<div class='card'>")
            if kind == "palette":
                cls = "swatch" if value is not None else "swatch unresolved"
                style = f" style='background:var({ref.token})'" if value is not None else ""
                parts.append(f"<div class='{cls}'{style}></div>")
            elif kind == "motion" and value is not None and "duration" in ref.token:
                parts.append(
                    "<div class='track'><div class='dot' style='animation-duration:"
                    f"var({ref.token});animation-timing-function:var(--qontinui-ease-standard,ease)'>"
                    "</div></div>"
                )
            elif kind == "motion" and value is not None and "ease" in ref.token:
                parts.append(
                    "<div class='track'><div class='dot' style='animation-duration:"
                    f"var(--qontinui-duration-deliberate,900ms);animation-timing-function:var({ref.token})'>"
                    "</div></div>"
                )
            elif kind == "typography" and value is not None and "font-size" in ref.token:
                parts.append(
                    f"<div style='font-size:var({ref.token});line-height:1.2'>Plan → land</div>"
                )
            parts.append(f"<code>{_e(ref.token)}</code>")
            parts.append(
                f"<code class='value'>{_e(value)}</code>"
                if value is not None
                else "<span class='value unknown'>unresolved</span>"
            )
            parts.append(f"<p>{_e(ref.role)}</p></div>")
        parts.append("</div>")

    parts.append("<h2>Palette</h2>")
    token_cards(guide.tokens.palette, "palette")
    parts.append("<h2>Motion</h2>")
    token_cards(guide.tokens.motion, "motion")
    parts.append(f"<p>Reduced motion: {_e(guide.tokens.reduced_motion)}</p>")
    parts.append("<h2>Typography</h2>")
    token_cards(guide.tokens.typography, "typography")

    m = guide.metaphor
    parts.append(f"<h2>The metaphor: {_e(m.name)}</h2><p>{_e(m.summary)}</p><ol>")
    parts.extend(f"<li>{_e(r)}</li>" for r in m.rules)
    parts.append("</ol>")
    if m.do:
        parts.append("<h3>Do</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in m.do) + "</ul>")
    if m.dont:
        parts.append("<h3>Don't</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in m.dont) + "</ul>")

    parts.append("<h2>Icons</h2><div class='grid'>")
    for icon in guide.icons:
        parts.append(
            f"<div class='card'><div class='icon' role='img' aria-label='{_e(icon.id)}'>"
            f"{icons[icon.id]}</div><code>{_e(icon.id)}</code><p>{_e(icon.meaning)}</p></div>"
        )
    parts.append("</div>")

    drift: list[str] = []
    palette_values = (
        {v.upper() for v in tokens.values() if HEX_RE.fullmatch(v)} if tokens is not None else None
    )
    parts.append("<h2>Looks (prompt fragments)</h2><div class='grid'>")
    for look in guide.looks:
        text = _inside(style_dir, look.prompt_fragment).read_text(encoding="utf-8").strip()
        hexes = sorted({h.upper() for h in HEX_RE.findall(text)})
        if palette_values is None:
            check = "<span class='unknown'>colours unchecked (no tokens)</span>"
        else:
            off = [h for h in hexes if h not in palette_values]
            drift.extend(f"{look.id}: {h}" for h in off)
            check = (
                f"<span class='fail'>colours not in the tokens: {_e(', '.join(off))}</span>"
                if off
                else f"<span class='pass'>{len(hexes)} colour(s), all token values</span>"
            )
        parts.append(
            f"<div class='card'><code>{_e(look.id)}</code> — <code>{_e(look.prompt_fragment)}"
            f"</code><p>{_e(look.used_for)}</p><p class='muted'>{_e(text)}</p><p>{check}</p></div>"
        )
    parts.append("</div>")

    r = guide.references
    tagged: list[AssetRecord] = library.tagged(r.tag) if library is not None else []
    approved = [a for a in tagged if a.approval_status == "approved"]
    parts.append(f"<h2>Reference set <code>{_e(r.tag)}</code></h2>")
    if library is None:
        parts.append("<p class='unknown'>No library was read: the reference set is UNKNOWN.</p>")
    else:
        verdict = "pass" if len(approved) >= r.target_approved else "fail"
        parts.append(
            f"<p class='{verdict}'>{len(approved)} approved of {len(tagged)} tagged; "
            f"the target is {r.target_approved} approved.</p>"
        )
    if r.note:
        parts.append(f"<p class='muted'>{_e(r.note)}</p>")
    if r.briefs:
        parts.append(
            "<p class='muted'>Briefs: "
            + ", ".join(f"<code>{_e(b)}</code>" for b in r.briefs)
            + "</p>"
        )
    if approved and library is not None:
        parts.append("<div class='refs'>")
        for a in approved:
            preview = next(
                (v.path for v in library.variants(a.uid) if v.format in ("webp", "png", "jpeg")),
                a.raw_path,
            )
            shown = refs_dir / f"{a.uid}{Path(preview).suffix}"
            shown.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(preview, shown)
            parts.append(
                f"<figure class='card' style='margin:0'><img src='refs/{_e(shown.name)}' "
                f"alt='{_e(a.asset_id)} reference' loading='lazy'><figcaption class='muted'>"
                f"{_e(a.uid)} · {_e(a.provider)}/{_e(a.model)} · approved by {_e(a.approval_by)} "
                f"on {_e(a.approval_on)}</figcaption></figure>"
            )
        parts.append("</div>")
    parts.append("</main></body></html>")
    page = out_dir / "index.html"
    page.write_text("".join(parts), encoding="utf-8")
    return StyleSheet(
        page=page,
        tokens_named=len(named),
        tokens_resolved=sum(1 for t in named if value_of(t.token) is not None),
        references_tagged=len(tagged),
        references_approved=len(approved),
        target_approved=r.target_approved,
        fragment_drift=drift,
    )
