"""The D3 allowlist validator — a PORT of qontinui-web's, kept in lock-step.

Source: qontinui-web ``backend/app/services/build_record_allowlist.py`` at
commit ``f300da6cd399577b860f88f1f1cb4405022cd975`` (file sha256 ``354a745de7e643f54636b3594c1945276bcaa4d560bf06ca23baea77470c6c66``). The body below is
that module (reformatted to this repo's line length; logic unchanged);
``tests/test_launch_allowlist_parity.py`` runs it over a fixture corpus
generated from the web module (``tests/fixtures/build_record_corpus.json``)
and fails on any difference. When the web validator changes, re-port it and
regenerate the corpus (``tests/fixtures/make_build_record_corpus.py``).

The original module docstring follows.

The D3 allowlist for a public build record, and the validator that enforces it.

Plan ``2026-10-09-factory-built-product-portfolio-and-launch-kit``, design
decision D3 ("public redaction is explicit, not implied") and Phase 1.

coord's ``coord_build_record`` composer applies this allowlist at its one exit.
qontinui-web applies it AGAIN before a document is frozen into
``web.build_record_snapshots``, and once more every time the public route
serves one — defence in depth, so a coord regression cannot reach the
unauthenticated reader. The key set is the binding cross-repo contract's
``build-record/1`` schema, key for key; ``tests/test_build_records.py`` pins it.

What the validator refuses
==========================

**Size**, before anything else: a document over 1 MiB serialized, and any
scanned string over 512 characters (both reported by slot). Every regex below
is written to run in linear time (anchored starts, possessive quantifiers),
so with the caps no input can make validation slow — the public route runs it
on every read.

**Shape.** Any key not in :data:`BUILD_RECORD_ALLOWLIST`, at any depth; a
container where a scalar belongs and the reverse.

**Per-slot value types.** Every leaf has a declared kind (:class:`Slot`):

* counts and the PR number — ``int`` or ``null`` (``bool`` refused, since it
  is an ``int`` subclass in Python);
* timestamps — an RFC 3339 string, or ``null`` where the contract allows it;
* statuses — coord's canonical work-unit vocabulary
  (:data:`WORK_UNIT_STATUSES`, ``WorkUnitStatus::from_wire`` in qontinui-coord
  ``work_unit_registry.rs``); a legacy opaque status is refused, never passed;
* repos — ``owner/name``; the product slug — the product slug pattern;
  work-unit slugs — :data:`WORK_UNIT_SLUG_RE` (plan stems are longer);
* PR and work-unit titles — any string or ``null`` (coord nulls a title that
  would fail the scan below and records ``<path>: not_established``);
* timestamps are PARSED, not only pattern-matched;
* ``unknowns[]`` and ``sessions.unknown_reason`` — ONLY the constrained form
  ``<allowlisted path>: <reason code>`` with a reason from
  :data:`UNKNOWN_REASON_CODES`. Free text is refused: it is the one slot an
  unvetted sentence (a device name, an operator, a findings body) would ride.

**Content, in every string anywhere** (titles included), read after NFKC
normalisation with lookalike slashes mapped to ``/`` and spaces around ``/``
removed (:func:`normalize_for_scan`):

* an ``owner/name`` token that is not one of ``product.repos`` — a private
  repo name leaking through a title, a slug or an unknown;
* a UUID-shaped substring, dashed or as a bare 32-hex run (device,
  session, user and tenant ids);
* an email-shaped substring, ``user@host`` included (operator identities).

Two layers against characters that hide a token. (1) A string carrying a
format, control, unassigned, private-use or surrogate character, any
``Default_Ignorable_Code_Point``, or a combining overlay (U+0334–U+0338,
U+20D2/3, U+20E5/6) is refused outright. (2) Every check runs on a STRIPPED
form anyway (:func:`normalize_for_scan`: NFKD, drop marks and invisibles,
NFKC, lookalike and infix-symbol slashes), so a character outside the refusal
set still cannot split a token. Neither layer is claimed complete; together
they close every probe in ``tests/test_build_records.py``.

:func:`names_token` is the token-bounded check the publish route also runs
for the NAMES of repos coord excluded (defined but not in the document).

**Cross-field.** A ``schema`` other than :data:`BUILD_RECORD_SCHEMA`; a PR
whose ``repo`` is not one of ``product.repos`` (coord lists only repos
POSITIVELY known public there).

A key the allowlist names but the document omits is not a violation: absence
leaks nothing. Violation messages name the SLOT, never the offending value
(an unlisted key is reported as ``<unlisted key>``), so a refusal cannot
itself leak what it refused.

Known false-positive class, accepted fail-closed: a title containing a
non-repo ``a/b`` token (``CI/CD``) is refused. Two all-digit segments
(``2026/10``) are exempt.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Any, Final

#: The one schema id this allowlist describes.
BUILD_RECORD_SCHEMA: Final = "build-record/1"

#: coord's canonical work-unit lifecycle (``WorkUnitStatus::from_wire``).
WORK_UNIT_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "draft",
        "in_progress",
        "blocked",
        "vetted",
        "ready",
        "shipped",
        "superseded",
        "obsolete",
    }
)

#: Why a field could not be established — the only words an unknown may use.
UNKNOWN_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "not_established",
        "door_unavailable",
        "excluded_not_known_public",
        "census_provisional",
        "not_applicable",
    }
)

#: The version of THIS allowlist and its scanner. A snapshot stores the
#: version it was validated under at publish; the public route re-validates
#: only a snapshot stored under a different version. BUMP IT on any change to
#: the key set, a slot rule, a pattern, normalisation or a cap.
ALLOWLIST_VERSION: Final = 14

#: Longest string the content scan will read — measured on the string AND on
#: its NFKD decomposition (which can be ~18× longer for one code point, e.g.
#: U+FDFA), and the decomposition may be at most 2× the original. Every real
#: title, slug and unknown is far shorter; a longer one is refused before any
#: normalisation or regex runs.
MAX_SCANNED_STRING_CHARS: Final = 512
MAX_NFKD_EXPANSION: Final = 2
#: Largest serialized document accepted (UTF-8 JSON). A real build record is
#: a few KiB per hundred work units; 256 KiB bounds the worst-case scan cost.
MAX_DOCUMENT_BYTES: Final = 256 * 1024

SLUG_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: A work-unit slug. ``coord.work_units.slug`` is unconstrained ``TEXT``
#: (``coord_workunits_01_work_units``), and real plan stems run past 140
#: characters and carry upper case, ``.`` and ``_`` (``PLAN_2026_06_17_…``,
#: ``….VETTED-INPROGRESS``) — so this is wider than the product slug. It still
#: forbids ``/`` and whitespace, and the content scan below still applies.
WORK_UNIT_SLUG_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")
REPO_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
RFC3339_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
#: A dashed UUID, or the same 128 bits as a bare 32-hex run. A 40-hex commit
#: sha is NOT matched (the run must be exactly 32 hex characters long).
_UUID_RE: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"
)
#: ``user@host`` is enough — an internal host needs no dot to identify a person.
#: Starts only at the beginning of a local-part run (lookbehind) and never
#: backtracks into it (possessive), so a long run with no ``@`` is O(n), not
#: O(n²) — every regex in this module is written to stay linear; see the
#: adversarial timing test in ``tests/test_build_records.py``.
_EMAIL_RE: Final = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@[A-Za-z0-9-]")
#: Characters that render as ``/`` but are not it (NFKC already folds U+FF0F
#: and U+FF89 to U+30CE, both listed anyway). Any OTHER non-ASCII math symbol
#: or "other punctuation" sitting between two ASCII alphanumerics is treated
#: as a slash too (:data:`_INFIX_SYMBOL_RE`).
_LOOKALIKE_SLASHES: Final = dict.fromkeys(
    map(
        ord,
        "\u2215\u2044\u29f8\u2571\u1735\u27cb\u2afb\u2afd\u30ce\uff89\u4e3f\u2f03",
    ),
    "/",
)
#: A non-ASCII character between two ASCII alphanumerics (spaces allowed);
#: :func:`_infix_to_slash` keeps it only when it is ``Sm`` or ``Po``.
_INFIX_SYMBOL_RE: Final = re.compile(r"(?<=[A-Za-z0-9])\s*+([^\x00-\x7f])\s*+(?=[A-Za-z0-9])")

#: A compact confusable skeleton (in the spirit of Unicode TR39) for the
#: scripts whose letters are drawn like Latin ones: Cyrillic and Greek
#: lookalikes → the Latin letter they render as. No confusables library is a
#: dependency of this backend; this table covers the letters an attacker can
#: actually type to make ``acme/secret`` scan as something else. Applied to the
#: scan form only — the stored document is never rewritten.
_CONFUSABLES: Final = str.maketrans(
    {
        # Cyrillic lower
        "\u0430": "a",
        "\u0432": "b",
        "\u0435": "e",
        "\u043a": "k",
        "\u043c": "m",
        "\u043d": "h",
        "\u043e": "o",
        "\u0440": "p",
        "\u0441": "c",
        "\u0442": "t",
        "\u0443": "y",
        "\u0445": "x",
        "\u0455": "s",
        "\u0456": "i",
        "\u0458": "j",
        "\u0491": "r",
        "\u04bb": "h",
        "\u0501": "d",
        "\u051b": "q",
        "\u051d": "w",
        "\u0475": "v",
        "\u04cf": "l",
        "\u0261": "g",
        # Cyrillic upper
        "\u0410": "A",
        "\u0412": "B",
        "\u0415": "E",
        "\u041a": "K",
        "\u041c": "M",
        "\u041d": "H",
        "\u041e": "O",
        "\u0420": "P",
        "\u0421": "C",
        "\u0422": "T",
        "\u0423": "Y",
        "\u0425": "X",
        "\u0405": "S",
        "\u0406": "I",
        "\u0408": "J",
        "\u04c0": "I",
        "\u051a": "Q",
        "\u051c": "W",
        # Greek lower
        "\u03b1": "a",
        "\u03b2": "b",
        "\u03b5": "e",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03bd": "v",
        "\u03bf": "o",
        "\u03c1": "p",
        "\u03c4": "t",
        "\u03c5": "u",
        "\u03c7": "x",
        "\u03b3": "y",
        # Greek upper
        "\u0391": "A",
        "\u0392": "B",
        "\u0395": "E",
        "\u0396": "Z",
        "\u0397": "H",
        "\u0399": "I",
        "\u039a": "K",
        "\u039c": "M",
        "\u039d": "N",
        "\u039f": "O",
        "\u03a1": "P",
        "\u03a4": "T",
        "\u03a5": "Y",
        "\u03a7": "X",
        # Latin-script lookalikes outside ASCII
        "\u0131": "i",
        "\u0269": "i",
        "\u01c0": "l",
    }
)
#: Every dash-punctuation (``Pd``) character, and U+2212 MINUS SIGN, scans as
#: ``-`` — so a UUID or repo name written with en dashes is still seen.
_EXTRA_DASHES: Final = frozenset("\u2212")

#: Unicode ``Default_Ignorable_Code_Point`` — characters that render as
#: NOTHING, whatever their general category (U+3164 is ``Lo``, the variation
#: selectors are ``Mn``). As (first, last) inclusive ranges.
DEFAULT_IGNORABLE_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
#: Categories a string is REFUSED for carrying (with the default-ignorables
#: and the overlays): format, control, unassigned, private use, surrogate.
_REFUSED_CATEGORIES: Final = frozenset({"Cf", "Cc", "Cn", "Co", "Cs"})
#: Categories dropped from the STRIPPED scan form (adds the combining marks,
#: which are not refused — an accent is legitimate — but must not split a
#: token: ``secre\u0301t`` scans as ``secret``).
_STRIPPED_CATEGORIES: Final = frozenset({"Mn", "Me", "Cf", "Cc", "Cn", "Co", "Cs"})
#: Starts only at the first whitespace of a run (or at the ``/`` itself) and
#: is possessive: ``\s*/\s*`` restarted at every position of a long
#: whitespace run, which was O(n²) (a 100k-space title took 14.7 s).
_SPACED_SLASH_RE: Final = re.compile(r"(?<!\s)\s*+/\s*+")
#: Combining marks that draw a stroke THROUGH the previous character, so
#: ``a\u0338b`` can render like ``a/b`` while scanning as two letters.
_OVERLAY_MARKS: Final = frozenset("\u0334\u0335\u0336\u0337\u0338\u20d2\u20d3\u20e5\u20e6")
_PATH_RUN_RE: Final = re.compile(r"[A-Za-z0-9._/-]+")
_UNKNOWN_RE: Final = re.compile(r"^([a-z_][a-z0-9_.\[\]]*+): ([a-z_]++)$")
_INDEX_RE: Final = re.compile(r"\[\d++\]")


class Slot(Enum):
    """The declared kind of one leaf."""

    SCHEMA = "schema"
    TIMESTAMP = "timestamp"
    TIMESTAMP_OR_NULL = "timestamp_or_null"
    COUNT = "count"
    REPO = "repo"
    SLUG = "slug"
    WORK_UNIT_SLUG = "work_unit_slug"
    STATUS = "status"
    STATUS_OR_NULL = "status_or_null"
    TEXT = "text"
    TEXT_OR_NULL = "text_or_null"
    UNKNOWN = "unknown"
    UNKNOWN_OR_NULL = "unknown_or_null"


#: The allowlist as a shape: a dict maps each allowed key to the shape of its
#: value, a one-element list means "a list whose items all have this shape",
#: and a :class:`Slot` is a leaf.
BUILD_RECORD_ALLOWLIST: Final[dict[str, Any]] = {
    "schema": Slot.SCHEMA,
    "generated_at": Slot.TIMESTAMP,
    "product": {
        "slug": Slot.SLUG,
        "title": Slot.TEXT,
        "repos": [Slot.REPO],
        "window_start": Slot.TIMESTAMP_OR_NULL,
        "window_end": Slot.TIMESTAMP_OR_NULL,
    },
    "work_units": [
        {
            "slug": Slot.WORK_UNIT_SLUG,
            "title": Slot.TEXT_OR_NULL,
            "status": Slot.STATUS,
            "vetted_at": Slot.TIMESTAMP_OR_NULL,
            "shipped_at": Slot.TIMESTAMP_OR_NULL,
        }
    ],
    "timeline": [
        {
            "at": Slot.TIMESTAMP,
            "work_unit": Slot.WORK_UNIT_SLUG,
            "from_status": Slot.STATUS_OR_NULL,
            "to_status": Slot.STATUS,
        }
    ],
    "prs": [
        {
            "repo": Slot.REPO,
            "number": Slot.COUNT,
            "title": Slot.TEXT_OR_NULL,
            "opened_at": Slot.TIMESTAMP_OR_NULL,
            "landed_at": Slot.TIMESTAMP_OR_NULL,
            "ci_duration_secs": Slot.COUNT,
            "attempts": Slot.COUNT,
        }
    ],
    "gates": {"registered": Slot.COUNT, "cleared": Slot.COUNT},
    "sessions": {"count": Slot.COUNT, "unknown_reason": Slot.UNKNOWN_OR_NULL},
    "wall_clock_secs": Slot.COUNT,
    "self_corrections": {
        "red_ci_fixed": Slot.COUNT,
        "review_findings_fixed": Slot.COUNT,
        "gate_reopens": Slot.COUNT,
    },
    "unknowns": [Slot.UNKNOWN],
}


def _key_paths(shape: Any, prefix: str) -> list[str]:
    if isinstance(shape, dict):
        paths: list[str] = []
        for key, sub in shape.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.append(path)
            paths.extend(_key_paths(sub, path))
        return paths
    if isinstance(shape, list):
        return _key_paths(shape[0], f"{prefix}[]")
    return []


#: Every allowed key, as a dotted path with ``[]`` for list items
#: (``prs[].repo``). The flat form a test pins against the contract.
BUILD_RECORD_ALLOWED_PATHS: Final[frozenset[str]] = frozenset(
    _key_paths(BUILD_RECORD_ALLOWLIST, "")
)


class BuildRecordRejected(ValueError):
    """The document failed the D3 allowlist. ``violations`` names every failure."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


def _is_count(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _is_unknown(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    match = _UNKNOWN_RE.fullmatch(value)
    if match is None:
        return False
    path, reason = match.groups()
    return (
        _INDEX_RE.sub("[]", path) in BUILD_RECORD_ALLOWED_PATHS and reason in UNKNOWN_REASON_CODES
    )


def _is_timestamp(value: str) -> bool:
    """RFC 3339 by shape AND by value: ``2026-13-40T…`` matches the regex and
    is still refused, because it does not parse."""
    if RFC3339_RE.fullmatch(value) is None:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _slot_ok(slot: Slot, value: Any) -> bool:
    if slot is Slot.SCHEMA:
        return isinstance(value, str) and value == BUILD_RECORD_SCHEMA
    if slot is Slot.COUNT:
        return _is_count(value)
    if slot is Slot.UNKNOWN:
        return _is_unknown(value)
    if slot is Slot.UNKNOWN_OR_NULL:
        return value is None or _is_unknown(value)
    if slot is Slot.TIMESTAMP_OR_NULL and value is None:
        return True
    if slot in (Slot.STATUS_OR_NULL, Slot.TEXT_OR_NULL) and value is None:
        return True
    if not isinstance(value, str):
        return False
    if slot in (Slot.TIMESTAMP, Slot.TIMESTAMP_OR_NULL):
        return _is_timestamp(value)
    if slot in (Slot.STATUS, Slot.STATUS_OR_NULL):
        return value in WORK_UNIT_STATUSES
    if slot is Slot.REPO:
        return REPO_RE.fullmatch(value) is not None
    if slot is Slot.SLUG:
        return SLUG_RE.fullmatch(value) is not None
    if slot is Slot.WORK_UNIT_SLUG:
        return WORK_UNIT_SLUG_RE.fullmatch(value) is not None
    return True  # TEXT / TEXT_OR_NULL — the content scan below still applies


def _walk(
    value: Any, shape: Any, path: str, out: list[str], strings: list[tuple[str, str]]
) -> None:
    where = path or "<root>"
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            out.append(f"{where}: expected an object, got {type(value).__name__}")
            return
        for key, sub in value.items():
            child = f"{path}.{key}" if path else str(key)
            if key not in shape:
                # Never echo the key itself: an unlisted key's NAME can be the
                # leak (a hostname, an email used as a map key).
                parent = f"{path}." if path else ""
                out.append(f"{parent}<unlisted key>: key is not in the build-record allowlist")
                continue
            _walk(sub, shape[key], child, out, strings)
        return
    if isinstance(shape, list):
        if not isinstance(value, list):
            out.append(f"{where}: expected a list, got {type(value).__name__}")
            return
        for index, item in enumerate(value):
            _walk(item, shape[0], f"{path}[{index}]", out, strings)
        return
    if isinstance(value, dict | list):
        out.append(f"{where}: expected a scalar, got {type(value).__name__}")
        return
    if isinstance(value, str) and _too_long(value):
        out.append(f"{where}: longer than {MAX_SCANNED_STRING_CHARS} characters")
        return
    if not _slot_ok(shape, value):
        out.append(f"{where}: not a valid {shape.value} value")
        return
    # The schema id is a fixed literal (``build-record/1`` has the shape of an
    # owner/name pair), already checked exactly; every other string is scanned.
    if isinstance(value, str) and shape is not Slot.SCHEMA:
        strings.append((where, value))


def _too_long(value: str) -> bool:
    """Over the cap as written, or once decomposed (NFKD), or expanding more
    than :data:`MAX_NFKD_EXPANSION`×. The raw check comes first, so NFKD only
    ever runs on a string already within the cap."""
    if len(value) > MAX_SCANNED_STRING_CHARS:
        return True
    decomposed = len(unicodedata.normalize("NFKD", value))
    return decomposed > MAX_SCANNED_STRING_CHARS or decomposed > (
        MAX_NFKD_EXPANSION * max(len(value), 1)
    )


def is_default_ignorable(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in DEFAULT_IGNORABLE_RANGES)


def _infix_to_slash(match: re.Match[str]) -> str:
    ch = match.group(1)
    if ch == "/" or unicodedata.category(ch) in ("Sm", "Po"):
        return "/"
    return match.group(0)


def normalize_for_scan(text: str) -> str:
    """The STRIPPED form every content check reads.

    NFKD, then every combining mark (``Mn``/``Me``), format, control,
    unassigned, private-use and surrogate character and every
    default-ignorable code point is DROPPED, then NFKC (folds fullwidth and
    other compatibility forms to ASCII), then the confusable skeleton
    (Cyrillic/Greek lookalikes → Latin) and every ``Pd`` dash → ``-``.
    Lookalike slashes, and any non-ASCII
    ``Sm``/``Po`` between two ASCII alphanumerics, become ``/``; whitespace
    around ``/`` is removed. So ``acme ∕ secret``, ``acme⟋secret`` and
    ``secre\u0301t`` all scan as what they render as.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(
        ch
        for ch in decomposed
        if unicodedata.category(ch) not in _STRIPPED_CATEGORIES and not is_default_ignorable(ch)
    )
    folded = unicodedata.normalize("NFKC", kept).translate(_CONFUSABLES)
    folded = "".join(
        "-" if unicodedata.category(ch) == "Pd" or ch in _EXTRA_DASHES else ch for ch in folded
    ).translate(_LOOKALIKE_SLASHES)
    folded = _INFIX_SYMBOL_RE.sub(_infix_to_slash, folded)
    return _SPACED_SLASH_RE.sub("/", folded)


def _repo_tokens(normalized: str) -> list[str]:
    """Every ``a/b`` pair in an ALREADY-normalised string, lowercased (GitHub
    names are case-blind)."""
    pairs: list[str] = []
    for run in _PATH_RUN_RE.findall(normalized):
        segments = [s.strip(".-") for s in run.split("/")]
        for left, right in zip(segments, segments[1:], strict=False):
            if not left or not right or (left.isdigit() and right.isdigit()):
                continue
            pairs.append(f"{left}/{right}".lower())
    return pairs


def _has_hidden_characters(text: str) -> bool:
    """Characters that render as nothing (format ``Cf``, control ``Cc``, every
    default-ignorable code point whatever its category), that have no agreed
    rendering at all (unassigned ``Cn``, private use ``Co``, surrogates
    ``Cs``), or that draw a slash which is not one (overlay marks). A scanned
    string carrying any of them is refused outright. The scans ALSO run on the
    stripped form (:func:`normalize_for_scan`), so this refusal is one layer,
    not the only one."""
    return any(
        unicodedata.category(ch) in _REFUSED_CATEGORIES
        or is_default_ignorable(ch)
        or ch in _OVERLAY_MARKS
        for ch in text
    )


def scanned_strings(document: Any) -> list[tuple[str, str]]:
    """``(slot, value)`` for every string the content scan reads — every
    allowlisted string except the fixed ``schema`` literal."""
    strings: list[tuple[str, str]] = []
    _walk(document, BUILD_RECORD_ALLOWLIST, "", [], strings)
    return strings


def names_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """ONE token-bounded, case-insensitive alternation over ``names``
    (escaped), matched against already-normalised lower-cased text."""
    escaped = sorted({re.escape(n.lower()) for n in names if n}, key=len, reverse=True)
    if not escaped:
        return None
    return re.compile(rf"(?<![a-z0-9])(?:{'|'.join(escaped)})(?![a-z0-9])")


def names_token(text: str, name: str) -> bool:
    """Whether the stripped form of ``text`` contains ``name`` as a whole token
    (case-insensitive; bounded by anything that is not a letter or digit)."""
    pattern = names_pattern([name])
    return pattern is not None and (pattern.search(normalize_for_scan(text).lower()) is not None)


def _too_large(document: Any) -> bool:
    try:
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return True
    return len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES


def build_record_violations(document: Any, *, excluded_names: Iterable[str] = ()) -> list[str]:
    """Every way ``document`` breaks the allowlist; empty when it is publishable.

    Size first: a document over :data:`MAX_DOCUMENT_BYTES` is refused without
    being walked, and a string over :data:`MAX_SCANNED_STRING_CHARS` is
    refused without being scanned — no input can make this function slow.

    ``excluded_names``: names of repos the product defines but the document
    left out (not known public). Every scanned string is normalised ONCE and
    checked against one alternation over all of them.
    """
    if _too_large(document):
        return [f"<root>: larger than {MAX_DOCUMENT_BYTES} bytes serialized"]
    out: list[str] = []
    strings: list[tuple[str, str]] = []
    _walk(document, BUILD_RECORD_ALLOWLIST, "", out, strings)
    if not isinstance(document, dict):
        return out
    if "schema" not in document:
        out.append(f"schema: required, must be {BUILD_RECORD_SCHEMA!r}")

    product = document.get("product")
    repos = product.get("repos") if isinstance(product, dict) else None
    public_repos = (
        {r.lower() for r in repos if isinstance(r, str)} if isinstance(repos, list) else set()
    )

    excluded = names_pattern(excluded_names)
    for where, text in strings:
        if _has_hidden_characters(text):
            out.append(f"{where}: contains an invisible, control or overlay character")
            continue
        normalized = normalize_for_scan(text)
        if excluded is not None and excluded.search(normalized.lower()):
            out.append(f"{where}: names a repo excluded as not known public")
        if any(token not in public_repos for token in _repo_tokens(normalized)):
            out.append(f"{where}: names an owner/name not in product.repos")
        if _UUID_RE.search(normalized):
            out.append(f"{where}: contains a UUID-shaped identifier")
        if _EMAIL_RE.search(normalized):
            out.append(f"{where}: contains an email-shaped identity")

    prs = document.get("prs")
    if isinstance(prs, list):
        for index, pr in enumerate(prs):
            if isinstance(pr, dict) and str(pr.get("repo", "")).lower() not in (public_repos):
                out.append(f"prs[{index}].repo: not one of product.repos, so not known public")
    return out


def validate_build_record(document: Any) -> dict[str, Any]:
    """Return ``document`` unchanged when it passes; raise :class:`BuildRecordRejected`."""
    violations = build_record_violations(document)
    if violations:
        raise BuildRecordRejected(violations)
    assert isinstance(document, dict)  # guaranteed by an empty violation list
    return document


_scan_limiter: Any = None


async def scan_off_loop(fn: Any, *args: Any) -> Any:
    """Run a CPU-bound scan in a worker thread under a DEDICATED small
    :class:`anyio.CapacityLimiter` (:data:`SCAN_CONCURRENCY`), never the shared
    default pool, so scans can neither block the event loop nor starve every
    other ``run_in_threadpool`` caller."""
    import anyio

    global _scan_limiter
    if _scan_limiter is None:
        _scan_limiter = anyio.CapacityLimiter(SCAN_CONCURRENCY)
    return await anyio.to_thread.run_sync(fn, *args, limiter=_scan_limiter)


#: Concurrent scans across the process (:func:`scan_off_loop`).
SCAN_CONCURRENCY: Final = 2
