"""The launch kit's D3 validator is qontinui-web's, ported (review rounds 1-2).

``tests/fixtures/build_record_corpus.json`` holds, per case, the exact
violation list qontinui-web's ``build_record_violations`` returned for the
document :func:`tests.fixtures.make_build_record_corpus.corpus` builds
(regenerate with that script). The port must agree on every case, and —
since the public route runs the validator on every read — must stay fast on
adversarial input (its caps and linear regexes)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from qontinui_studio.launch import allowlist
from qontinui_studio.launch.allowlist import build_record_violations
from qontinui_studio.launch.record import BuildRecordInvalid, parse_document

from .fixtures.make_build_record_corpus import BASE, corpus

VERDICTS = json.loads(
    (Path(__file__).parent / "fixtures" / "build_record_corpus.json").read_text(encoding="utf-8")
)
CASES = corpus()


def test_corpus_names_its_source() -> None:
    assert VERDICTS["commit"] in (allowlist.__doc__ or "")
    assert VERDICTS["sha256"] in (allowlist.__doc__ or "")
    assert {c["name"] for c in CASES} == set(VERDICTS["violations"])
    assert len(CASES) >= 55


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_port_matches_qontinui_web(case: dict[str, Any]) -> None:
    expected = VERDICTS["violations"][case["name"]]
    excluded = case.get("excluded_names", ())
    assert build_record_violations(case["document"], excluded_names=excluded) == expected
    if excluded:
        return  # the excluded-name scan needs the product; parse_document has none
    if expected:
        with pytest.raises(BuildRecordInvalid):
            parse_document(case["document"])
    else:
        parse_document(case["document"])


def _cost(make: Callable[[int], str], n: int, repeat: int) -> float:
    """CPU seconds (process_time, immune to wall-clock noise) to validate a
    document whose PR title is ``make(n)``, ``repeat`` times."""
    doc = json.loads(json.dumps(BASE))
    doc["prs"][0]["title"] = make(n)
    started = time.process_time()
    for _ in range(repeat):
        build_record_violations(doc)
    return time.process_time() - started


ADVERSARIAL: dict[str, Callable[[int], str]] = {
    "spaces": lambda n: " " * n + "x",
    "local-part": lambda n: "a" * n,
    "spaced-slashes": lambda n: " /" * (n // 2),
    "at-run": lambda n: "a@" * (n // 2),
    "lookalike-slashes": lambda n: "a" + "\u2215 " * (n // 2),
    "nfkd-expanding": lambda n: "\ufb01" * (n // 2),
}


@pytest.mark.parametrize("make", ADVERSARIAL.values(), ids=list(ADVERSARIAL))
def test_scan_cost_is_linear_below_the_cap(make: Callable[[int], str]) -> None:
    """Review round 2 finding 4: every regex runs in linear time. Doubling the
    input (still under the 512-char cap) must not much more than double the
    CPU cost — a quadratic scan would quadruple it."""
    _cost(make, 128, 50)  # warm caches
    small = min(_cost(make, 128, 200) for _ in range(3))
    large = min(_cost(make, 256, 200) for _ in range(3))
    assert large / max(small, 1e-9) < 3.0, (small, large)


@pytest.mark.parametrize("make", ADVERSARIAL.values(), ids=list(ADVERSARIAL))
def test_over_the_cap_is_refused_at_linear_cost(make: Callable[[int], str]) -> None:
    """Over the cap the string is refused before any regex runs: the cost of
    a 2n input is at most about twice that of an n input."""
    small = min(_cost(make, 50_000, 5) for _ in range(3))
    large = min(_cost(make, 100_000, 5) for _ in range(3))
    assert large / max(small, 1e-9) < 3.0, (small, large)
    doc = json.loads(json.dumps(BASE))
    doc["prs"][0]["title"] = make(100_000)
    assert "prs[0].title: longer than 512 characters" in build_record_violations(doc)


def test_documents_over_256_kib_are_refused() -> None:
    for name, refused in (
        ("document-over-256kib", True),
        ("document-at-256kib", False),
        ("document-under-256kib", False),
    ):
        case = next(c for c in CASES if c["name"] == name)
        violations = build_record_violations(case["document"])
        assert ("<root>: larger than 262144 bytes serialized" in violations) is refused, name
