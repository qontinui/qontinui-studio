"""Pillow renders: OG/social cards and the video's build-record slides.

Text on every card comes from the product definition (name, tagline) and the
build record's :class:`Fact` values — nothing else. Unknowns render as
``unknown``, never 0. Uses Pillow's bundled FreeType font, so no system font
is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from qontinui_studio.launch.facts import Fact

BG = (18, 20, 28)
FG = (240, 241, 245)
MUTED = (150, 156, 172)
ACCENT = (110, 168, 255)
FOOTER = "Built by the Qontinui software factory"


@dataclass(frozen=True)
class CardSize:
    name: str
    width: int
    height: int


#: OG and per-network social card sizes.
SOCIAL_CARDS: tuple[CardSize, ...] = (
    CardSize("og", 1200, 630),
    CardSize("x", 1200, 675),
    CardSize("linkedin", 1200, 627),
    CardSize("square", 1080, 1080),
)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _safe(text: str) -> str:
    """The bundled font lacks a few typographic glyphs; spell them in ASCII."""
    return text.replace("≥ ", "at least ").replace("—", "-").replace("’", "'")


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: int,
) -> list[str]:
    words = _safe(text).split()
    lines: list[str] = []
    line = ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _text_block(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int,
    width: int,
    fill: tuple[int, int, int],
    max_lines: int,
) -> int:
    """Draw wrapped text; return the y below it."""
    font = _font(size)
    x, y = xy
    lines = _wrap(draw, text, font, width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".") + "..."
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += int(size * 1.25)
    return y


def social_card(size: CardSize, title: str, tagline: str, facts: list[Fact], dest: Path) -> Path:
    """A card: product name, tagline, up to three KNOWN headline facts, footer."""
    img = Image.new("RGB", (size.width, size.height), BG)
    draw = ImageDraw.Draw(img)
    pad = size.width // 15
    inner = size.width - 2 * pad
    draw.rectangle((0, 0, size.width, 10), fill=ACCENT)
    y = _text_block(draw, (pad, pad), title, size.width // 16, inner, FG, 2)
    y = _text_block(draw, (pad, y + 12), tagline, size.width // 34, inner, MUTED, 3)
    known = [f for f in facts if f.known][:3]
    if known:
        col = inner // len(known)
        top = y + size.height // 14
        for i, fact in enumerate(known):
            x = pad + i * col
            _text_block(draw, (x, top), fact.short(), size.width // 20, col - 16, ACCENT, 1)
            _text_block(
                draw, (x, top + size.width // 16), fact.label, size.width // 45, col - 16, MUTED, 2
            )
    footer_font = _font(size.width // 48)
    draw.text((pad, size.height - pad), FOOTER, font=footer_font, fill=MUTED)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    return dest


def title_slide(title: str, subtitle: str, dest: Path, width: int, height: int) -> Path:
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    pad = width // 12
    draw.rectangle((0, 0, width, 8), fill=ACCENT)
    y = _text_block(draw, (pad, height // 3), title, width // 14, width - 2 * pad, FG, 2)
    _text_block(draw, (pad, y + 16), subtitle, width // 36, width - 2 * pad, MUTED, 4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    return dest


def facts_slide(heading: str, facts: list[Fact], dest: Path, width: int, height: int) -> Path:
    """A build-record summary slide: label, value (or ``unknown``), and the
    document field it came from."""
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    pad = width // 14
    draw.rectangle((0, 0, width, 8), fill=ACCENT)
    y = _text_block(draw, (pad, pad), heading, width // 26, width - 2 * pad, FG, 1) + 20
    row = (height - y - pad) // max(len(facts), 1)
    label_font = _font(max(width // 48, 12))
    value_font = _font(max(width // 30, 14))
    source_font = _font(max(width // 80, 10))
    for fact in facts:
        draw.text((pad, y), _safe(fact.label), font=label_font, fill=MUTED)
        draw.text(
            (width // 2, y - 4),
            _safe(fact.short()),
            font=value_font,
            fill=ACCENT if fact.known else MUTED,
        )
        draw.text(
            (width // 2, y + width // 28), f"from {fact.source}", font=source_font, fill=MUTED
        )
        y += row
    draw.text((pad, height - pad // 2 - 10), FOOTER, font=source_font, fill=MUTED)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    return dest


def fit_frame(src: Path, dest: Path, width: int, height: int, label: str | None = None) -> Path:
    """Letterbox an image onto a ``width``x``height`` frame (no crop: a
    screenshot keeps all of its pixels). ``label`` is burned into the corner —
    used to mark AI b-roll as AI-generated inside the film itself (D6)."""
    with Image.open(src) as opened:
        img = opened.convert("RGB")
    framed = ImageOps.pad(img, (width, height), color=BG)
    if label:
        draw = ImageDraw.Draw(framed)
        font = _font(max(width // 50, 12))
        text = _safe(label)
        tw = int(draw.textlength(text, font=font))
        x, y = width - tw - 24, height - width // 50 - 24
        draw.rectangle((x - 10, y - 6, x + tw + 10, y + width // 50 + 8), fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=FG)
    dest.parent.mkdir(parents=True, exist_ok=True)
    framed.save(dest, "PNG")
    return dest
