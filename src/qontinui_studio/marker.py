"""The AI-generated marker: IPTC ``DigitalSourceType`` = ``trainedAlgorithmicMedia``,
written as XMP into every image file the studio generates or derives (decision
record ``image-generation-weights-are-permissively-licensed``, a5ec7670 — its
mandatory marking part; plan 2026-10-13 Phase 11 step 4).

Two writers, one reader:

* :func:`mark_bytes` marks a provider's raw bytes WITHOUT re-encoding them when
  they are PNG (an ``iTXt`` chunk) or JPEG (an ``APP1`` segment). Any other
  format is transcoded losslessly to PNG, so the pixels are exactly the ones
  the provider returned.
* :func:`save_options` gives Pillow the arguments that embed the packet when a
  derivative is encoded (PNG via ``pnginfo``, WebP/AVIF/JPEG via ``xmp``).
* :func:`is_marked` reads the packet back through Pillow's own XMP parsing —
  the marker must sit where XMP readers look, not merely somewhere in the bytes.

C2PA is not written: it needs a signing credential, and obtaining a trusted one
is a billing decision this plan does not take. The IPTC marker meets a5ec7670's
mandatory part.
"""

from __future__ import annotations

import io
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


def _png_itxt() -> bytes:
    # keyword NUL, compression flag 0, method 0, empty language tag NUL, empty
    # translated keyword NUL, then the uncompressed UTF-8 text.
    payload = _PNG_XMP_KEYWORD.encode("latin-1") + b"\x00\x00\x00\x00\x00" + XMP_PACKET
    return _png_chunk(b"iTXt", payload)


def _mark_png(data: bytes) -> bytes:
    """Insert the XMP ``iTXt`` chunk right after ``IHDR``; no pixel is touched."""
    if not data.startswith(_PNG_SIGNATURE) or len(data) < 33:
        raise MarkerError("not a PNG")
    (ihdr_len,) = struct.unpack(">I", data[8:12])
    end_of_ihdr = 8 + 12 + ihdr_len
    return data[:end_of_ihdr] + _png_itxt() + data[end_of_ihdr:]


def _mark_jpeg(data: bytes) -> bytes:
    """Insert an XMP ``APP1`` segment after SOI (and after a leading JFIF APP0,
    which must stay first)."""
    if not data.startswith(b"\xff\xd8"):
        raise MarkerError("not a JPEG")
    payload = _JPEG_XMP_NAMESPACE + XMP_PACKET
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    pos = 2
    if data[2:4] == b"\xff\xe0":
        (app0_len,) = struct.unpack(">H", data[4:6])
        pos = 4 + app0_len
    return data[:pos] + segment + data[pos:]


def _xmp_of(img: Image.Image) -> bytes:
    raw = img.info.get("xmp")
    if raw is None and img.format == "PNG":
        raw = img.info.get(_PNG_XMP_KEYWORD)
    if raw is None:
        return b""
    return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")


def bytes_are_marked(data: bytes) -> bool:
    """Whether ``data`` is an image whose XMP declares trainedAlgorithmicMedia."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            return DIGITAL_SOURCE_TYPE.encode() in _xmp_of(img)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return False


def is_marked(path: Path) -> bool:
    try:
        return bytes_are_marked(path.read_bytes())
    except OSError:
        return False


def mark_bytes(data: bytes) -> bytes:
    """Return ``data`` carrying the marker. Already-marked bytes are returned
    unchanged (the local facade marks its PNGs itself)."""
    if bytes_are_marked(data):
        return data
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            if fmt == "PNG":
                return _mark_png(data)
            if fmt == "JPEG":
                return _mark_jpeg(data)
            img.load()
            out = io.BytesIO()
            frame = img.convert("RGBA") if img.mode not in ("RGB", "RGBA", "L", "LA") else img
            frame.save(out, "PNG", **save_options("png"))
            return out.getvalue()
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise MarkerError(f"cannot mark bytes that are not an image: {e}") from e


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
