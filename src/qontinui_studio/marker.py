"""The AI-generated marker: IPTC ``DigitalSourceType`` = ``trainedAlgorithmicMedia``,
written as XMP into every image file the studio generates or derives (decision
record ``image-generation-weights-are-permissively-licensed``, a5ec7670 — its
mandatory marking part; plan 2026-10-13 Phase 11 step 4).

Two writers, one reader:

* :func:`mark_bytes` marks a provider's raw bytes WITHOUT re-encoding them when
  they are PNG (an ``iTXt`` chunk) or JPEG (an ``APP1`` segment), MERGING the
  marker into any XMP packet the provider wrote (the file keeps exactly one).
  Any other format is transcoded losslessly to PNG, so the pixels are exactly
  the ones the provider returned. The result is read back; a file that still
  does not read as marked raises :class:`MarkerError`.
* :func:`save_options` gives Pillow the arguments that embed the packet when a
  derivative is encoded (PNG via ``pnginfo``, WebP/AVIF/JPEG via ``xmp``).
* :func:`is_marked` reads the format's REAL XMP container — a PNG ``iTXt``
  chunk, a JPEG ``APP1`` segment (parsed here), a WebP/AVIF XMP item (through
  Pillow) — never a ``tEXt`` chunk that merely borrows the keyword. It requires
  exactly one packet, whose ``DigitalSourceType`` property is the marker.

C2PA is not written: it needs a signing credential, and obtaining a trusted one
is a billing decision this plan does not take. The IPTC marker meets a5ec7670's
mandatory part.
"""

from __future__ import annotations

import io
import re
import struct
import zlib
from pathlib import Path

from PIL import Image, PngImagePlugin, UnidentifiedImageError

DIGITAL_SOURCE_TYPE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"

XMP_PACKET = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" '
    'xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
    f'Iptc4xmpExt:DigitalSourceType="{DIGITAL_SOURCE_TYPE}"/>'
    "</rdf:RDF></x:xmpmeta>"
    '<?xpacket end="w"?>'
).encode()

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_XMP_KEYWORD = "XML:com.adobe.xmp"
_JPEG_XMP_NAMESPACE = b"http://ns.adobe.com/xap/1.0/\x00"


class MarkerError(ValueError):
    """The bytes are not an image the marker can be written into."""


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_itxt(packet: bytes) -> bytes:
    # keyword NUL, compression flag 0, method 0, empty language tag NUL, empty
    # translated keyword NUL, then the uncompressed UTF-8 text.
    payload = _PNG_XMP_KEYWORD.encode("latin-1") + b"\x00\x00\x00\x00\x00" + packet
    return _png_chunk(b"iTXt", payload)


def _png_chunks(data: bytes) -> list[tuple[int, int, bytes, bytes]]:
    """(start, end, type, payload) of every chunk; raises on a truncated file."""
    if not data.startswith(_PNG_SIGNATURE):
        raise MarkerError("not a PNG")
    out: list[tuple[int, int, bytes, bytes]] = []
    pos = 8
    while pos < len(data):
        if pos + 12 > len(data):
            raise MarkerError("truncated PNG chunk")
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise MarkerError("truncated PNG chunk")
        out.append((pos, end, kind, data[pos + 8 : pos + 8 + length]))
        pos = end
        if kind == b"IEND":
            break
    return out


def _png_itxt_xmp(payload: bytes) -> bytes | None:
    """The XMP packet of an ``iTXt`` chunk keyed ``XML:com.adobe.xmp``, else None.
    A ``tEXt``/``zTXt`` chunk with that keyword is NOT an XMP container (the PNG
    and XMP specifications both say iTXt), so it is never read as one."""
    keyword, sep, rest = payload.partition(b"\x00")
    if not sep or keyword != _PNG_XMP_KEYWORD.encode("latin-1") or len(rest) < 2:
        return None
    flag, method, rest = rest[0], rest[1], rest[2:]
    _lang, _, rest = rest.partition(b"\x00")
    _translated, _, text = rest.partition(b"\x00")
    if flag == 0:
        return text
    if flag == 1 and method == 0:
        try:
            return zlib.decompress(text)
        except zlib.error:
            return None
    return None


def _jpeg_segments(data: bytes) -> list[tuple[int, int, int, bytes]]:
    """(start, end, marker, payload) of each header segment up to SOS."""
    if not data.startswith(b"\xff\xd8"):
        raise MarkerError("not a JPEG")
    out: list[tuple[int, int, int, bytes]] = []
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker_byte = data[pos + 1]
        if marker_byte == 0xDA:  # start of scan: header segments end here
            break
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        end = pos + 2 + length
        if length < 2 or end > len(data):
            raise MarkerError("truncated JPEG segment")
        out.append((pos, end, marker_byte, data[pos + 4 : end]))
        pos = end
    return out


def _packets(data: bytes) -> list[bytes] | None:
    """Every XMP packet in a PNG (``iTXt``) or JPEG (``APP1``) file, or None for
    another format (read through Pillow's own XMP container support)."""
    if data.startswith(_PNG_SIGNATURE):
        found = [_png_itxt_xmp(p) for _, _, kind, p in _png_chunks(data) if kind == b"iTXt"]
        return [x for x in found if x is not None]
    if data.startswith(b"\xff\xd8"):
        return [
            p[len(_JPEG_XMP_NAMESPACE) :]
            for _, _, m, p in _jpeg_segments(data)
            if m == 0xE1 and p.startswith(_JPEG_XMP_NAMESPACE)
        ]
    return None


_DST_ATTR = re.compile(r"((?:[A-Za-z_][\w.-]*:)?DigitalSourceType\s*=\s*)([\"'])(.*?)\2", re.S)
_DST_ELEM = re.compile(r"(<((?:[A-Za-z_][\w.-]*:)?DigitalSourceType)>)(.*?)(</\2>)", re.S)


def _declares(packet: bytes) -> bool:
    text = packet.decode("utf-8", "replace")
    values = [m.group(3).strip() for m in _DST_ATTR.finditer(text)]
    values += [m.group(3).strip() for m in _DST_ELEM.finditer(text)]
    return DIGITAL_SOURCE_TYPE in values


def bytes_are_marked(data: bytes) -> bool:
    """Whether ``data`` is an image carrying EXACTLY ONE XMP packet (in the
    format's real XMP container) whose ``DigitalSourceType`` property is
    trainedAlgorithmicMedia. Two packets are ambiguous — readers disagree on
    which one wins — so they never count as marked."""
    try:
        packets = _packets(data)
        if packets is None:
            with Image.open(io.BytesIO(data)) as img:
                raw = img.info.get("xmp")
            if raw is None:
                return False
            packets = [raw if isinstance(raw, bytes) else str(raw).encode("utf-8")]
    except (MarkerError, UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return False
    return len(packets) == 1 and _declares(packets[0])


def is_marked(path: Path) -> bool:
    try:
        return bytes_are_marked(path.read_bytes())
    except OSError:
        return False


def merged_packet(existing: bytes | None) -> bytes:
    """``existing`` with the marker merged in, or the marker's own packet.

    An existing ``DigitalSourceType`` is set to trainedAlgorithmicMedia (the
    studio generated these pixels); otherwise one more ``rdf:Description`` is
    added inside ``rdf:RDF`` (RDF allows any number), so the provider's other
    metadata is kept. A packet with no ``rdf:RDF`` cannot be merged into and is
    replaced."""
    if not existing:
        return XMP_PACKET
    text = existing.decode("utf-8", "replace")
    text, n_attr = _DST_ATTR.subn(
        lambda m: f"{m.group(1)}{m.group(2)}{DIGITAL_SOURCE_TYPE}{m.group(2)}", text
    )
    text, n_elem = _DST_ELEM.subn(lambda m: f"{m.group(1)}{DIGITAL_SOURCE_TYPE}{m.group(4)}", text)
    if n_attr or n_elem:
        return text.encode("utf-8")
    close = text.rfind("</rdf:RDF>")
    if close < 0:
        return XMP_PACKET
    description = (
        '<rdf:Description rdf:about="" '
        'xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
        f'Iptc4xmpExt:DigitalSourceType="{DIGITAL_SOURCE_TYPE}"/>'
    )
    return (text[:close] + description + text[close:]).encode("utf-8")


def _mark_png(data: bytes) -> bytes:
    """Replace every XMP ``iTXt`` chunk with ONE carrying the merged packet,
    right after ``IHDR``; no pixel is touched."""
    chunks = _png_chunks(data)
    if not chunks or chunks[0][2] != b"IHDR":
        raise MarkerError("PNG does not start with IHDR")
    xmp = [
        (a, b, p) for a, b, kind, p in chunks if kind == b"iTXt" and _png_itxt_xmp(p) is not None
    ]
    existing = _png_itxt_xmp(xmp[-1][2]) if xmp else None
    drop = {(a, b) for a, b, _ in xmp}
    body = b"".join(data[a:b] for a, b, _k, _p in chunks if (a, b) not in drop)
    first_end = chunks[0][1] - 8  # IHDR's end, relative to body
    tail = data[chunks[-1][1] :]
    return (
        _PNG_SIGNATURE
        + body[:first_end]
        + _png_itxt(merged_packet(existing))
        + body[first_end:]
        + tail
    )


def _mark_jpeg(data: bytes) -> bytes:
    """Replace every XMP ``APP1`` segment with ONE carrying the merged packet,
    after SOI (and after a leading JFIF APP0, which must stay first)."""
    segments = _jpeg_segments(data)
    xmp = [(a, b, p) for a, b, m, p in segments if m == 0xE1 and p.startswith(_JPEG_XMP_NAMESPACE)]
    existing = xmp[-1][2][len(_JPEG_XMP_NAMESPACE) :] if xmp else None
    for a, b, _ in reversed(xmp):
        data = data[:a] + data[b:]
    payload = _JPEG_XMP_NAMESPACE + merged_packet(existing)
    if len(payload) + 2 > 0xFFFF:
        raise MarkerError("the merged XMP packet does not fit one APP1 segment")
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    pos = 2
    if data[2:4] == b"\xff\xe0":
        (app0_len,) = struct.unpack(">H", data[4:6])
        pos = 4 + app0_len
    return data[:pos] + segment + data[pos:]


def mark_bytes(data: bytes) -> bytes:
    """Return ``data`` carrying the marker, or raise :class:`MarkerError`.

    Already-marked bytes are returned unchanged (the local facade marks its
    PNGs itself). PNG and JPEG are marked without re-encoding, merging into any
    XMP the provider wrote; another format is transcoded losslessly to PNG with
    its XMP merged. The result is READ BACK: bytes that still do not read as
    marked raise, so a job fails before it is staged, never at publish."""
    if bytes_are_marked(data):
        return data
    try:
        if data.startswith(_PNG_SIGNATURE):
            marked = _mark_png(data)
        elif data.startswith(b"\xff\xd8"):
            marked = _mark_jpeg(data)
        else:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                raw = img.info.get("xmp")
                existing = raw if isinstance(raw, bytes) else (str(raw).encode() if raw else None)
                out = io.BytesIO()
                frame = img.convert("RGBA") if img.mode not in ("RGB", "RGBA", "L", "LA") else img
                info = PngImagePlugin.PngInfo()
                info.add_itxt(_PNG_XMP_KEYWORD, merged_packet(existing).decode("utf-8"), zip=False)
                frame.save(out, "PNG", pnginfo=info)
                marked = out.getvalue()
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise MarkerError(f"cannot mark bytes that are not an image: {e}") from e
    if not bytes_are_marked(marked):
        raise MarkerError("the AI-generated marker did not read back after marking")
    return marked


def save_options(fmt: str) -> dict[str, object]:
    """Pillow ``save`` keyword arguments that embed the marker for ``fmt``
    (``png``, ``webp``, ``avif``, ``jpeg``)."""
    if fmt == "png":
        info = PngImagePlugin.PngInfo()
        info.add_itxt(_PNG_XMP_KEYWORD, XMP_PACKET.decode("utf-8"), zip=False)
        return {"pnginfo": info}
    if fmt in ("webp", "avif", "jpeg"):
        return {"xmp": XMP_PACKET}
    raise MarkerError(f"no marker writer for format '{fmt}'")
