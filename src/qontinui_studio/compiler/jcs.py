"""RFC 8785 (JSON Canonicalization Scheme) — the canonical form an image-slot
spec's content hash is taken over.

qontinui-schemas defines the hash (``ImageSlotSpec::content_sha256``,
``rust/src/image_slot.rs``): lowercase hex sha256 of the spec's RFC 8785
canonical JSON with ``provenance`` left out, where a pydantic model's JSON form
is ``model_dump(mode="json", by_alias=True, exclude_none=True,
exclude={"provenance"})``. Its valid fixtures pin their hashes on a
``# content-sha256:`` header line, and studio's tests check this module against
every one of them.

The rules, as RFC 8785 states them:

* object members sorted by their names as UTF-16 code-unit sequences;
* no whitespace;
* strings as ECMAScript ``JSON.stringify`` writes them: ``"`` and ``\\``
  escaped, the control characters U+0000-U+001F escaped (``\\b \\t \\n \\f \\r``
  short, the rest ``\\u00xx`` in lowercase hex), everything else literal UTF-8;
* numbers as ECMAScript ``Number.prototype.toString`` writes them (``1.0`` is
  ``1``, ``1e-7`` is ``1e-7``, ``1e21`` is ``1e+21``).

What RFC 8785 cannot carry is refused, never rewritten: NaN and the
infinities, an integer beyond ±(2^53 − 1), and a string holding a lone
surrogate (it has no UTF-8 form).
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class CanonicalizationError(ValueError):
    """The value holds something RFC 8785 cannot carry exactly."""


def _string(value: str, path: str) -> str:
    out = ['"']
    for ch in value:
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        elif 0xD800 <= ord(ch) <= 0xDFFF:
            raise CanonicalizationError(f"{path}: a lone surrogate has no UTF-8 form")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _float(value: float, path: str) -> str:
    """ECMAScript ``Number::toString`` for a finite double."""
    if not math.isfinite(value):
        raise CanonicalizationError(f"{path}: NaN or infinity has no RFC 8785 form")
    if value == 0:
        return "0"  # -0 serialises as 0
    sign = "-" if value < 0 else ""
    # repr gives the shortest digit string that round-trips, which is exactly
    # the "k as small as possible" digit string the ECMAScript algorithm uses.
    text = repr(abs(value))
    mantissa, _, exp_text = text.partition("e")
    exponent = int(exp_text) if exp_text else 0
    whole, _, frac = mantissa.partition(".")
    digits = (whole + frac).lstrip("0")
    # The decimal point sits after len(whole) digits of whole+frac; leading
    # zeros stripped from the digit string move it left.
    leading_zeros = len(whole + frac) - len((whole + frac).lstrip("0"))
    n = len(whole) - leading_zeros + exponent
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    exp = f"e{'+' if e >= 0 else '-'}{abs(e)}"
    if k == 1:
        return sign + digits + exp
    return sign + digits[0] + "." + digits[1:] + exp


def _serialise(value: Any, path: str) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalizationError(
                f"{path}: {value} is beyond 2^53 - 1, which RFC 8785 does not carry exactly"
            )
        return str(value)
    if isinstance(value, float):
        return _float(value, path)
    if isinstance(value, str):
        return _string(value, path)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialise(v, f"{path}[{i}]") for i, v in enumerate(value)) + "]"
    if isinstance(value, dict):
        members = []
        for key in sorted(value, key=_utf16_key):
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object key {key!r} is not a string")
            members.append(
                _string(key, path) + ":" + _serialise(value[key], f"{path}.{key}" if path else key)
            )
        return "{" + ",".join(members) + "}"
    raise CanonicalizationError(f"{path}: {type(value).__name__} is not a JSON value")


def _utf16_key(key: object) -> bytes:
    if not isinstance(key, str):
        raise CanonicalizationError(f"object key {key!r} is not a string")
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as e:
        raise CanonicalizationError(f"object key {key!r}: a lone surrogate") from e


def canonicalize(value: Any) -> str:
    """``value`` (plain JSON data: dict, list, str, int, float, bool, None) as
    RFC 8785 canonical JSON text."""
    return _serialise(value, "")


def canonical_sha256(value: Any) -> str:
    """Lowercase hex sha256 of ``value``'s canonical UTF-8 bytes."""
    return hashlib.sha256(canonicalize(value).encode("utf-8")).hexdigest()
