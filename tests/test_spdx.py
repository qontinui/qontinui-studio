"""The SPDX reader studio applies to a facade result's ``licence.spdx``: a
port of qontinui's parser (``docker/imagegen/qontinui_imagegen/manifest.py``
at a9dbb1a69ee03b3887b29defa21781c2ae1cb38e), with OR deliberately refused."""

from __future__ import annotations

import pytest

from qontinui_studio import provenance
from qontinui_studio.spdx import (
    ADMITTED_EXCEPTIONS,
    ADMITTED_SPDX,
    OR_REFUSED,
    SpdxError,
    spdx_blockers,
)

# qontinui's admitted set at a9dbb1a6 (decision record a5ec7670 v3): a change
# there is a deliberate change here.
QONTINUI_IDS = {
    "Apache-2.0",
    "MIT",
    "MIT-0",
    "MIT-CMU",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "0BSD",
    "BSL-1.0",
    "Zlib",
    "libpng-2.0",
    "IJG",
}


def test_the_admitted_set_is_qontinuis() -> None:
    assert set(ADMITTED_SPDX) == QONTINUI_IDS
    assert ADMITTED_EXCEPTIONS == {"Apache-2.0": frozenset({"LLVM-exception"})}


@pytest.mark.parametrize("identifier", sorted(QONTINUI_IDS))
def test_each_admitted_id_is_admitted(identifier: str) -> None:
    assert spdx_blockers(identifier) == []


@pytest.mark.parametrize("identifier", sorted(QONTINUI_IDS))
def test_or_later_is_a_choice_and_refused_like_or(identifier: str) -> None:
    """``X+`` is "X or any later version": a choice of licence, as OR is."""
    assert spdx_blockers(identifier + "+") == [f"{identifier}+: {OR_REFUSED}"]


@pytest.mark.parametrize(
    "expression",
    ["Apache-2.0+ WITH LLVM-exception", "MIT AND Zlib+", "(Apache-2.0 AND MIT+) AND IJG"],
)
def test_or_later_inside_an_expression_is_refused(expression: str) -> None:
    [blocked] = spdx_blockers(expression)
    assert blocked.endswith(OR_REFUSED) and "+" in blocked


@pytest.mark.parametrize(
    "expression",
    [
        "Apache-2.0 AND MIT",
        "(Apache-2.0 AND MIT) AND BSD-3-Clause",
        "Apache-2.0 AND (MIT AND (Zlib AND IJG))",
        "  Apache-2.0   AND\tMIT ",
        "Apache-2.0 WITH LLVM-exception",
        "(Apache-2.0 WITH LLVM-exception) AND MIT",
        "MIT AND Apache-2.0 WITH LLVM-exception",
    ],
)
def test_and_groups_and_the_admitted_exception_are_admitted(expression: str) -> None:
    assert spdx_blockers(expression) == []


@pytest.mark.parametrize(
    ("expression", "blocked"),
    [
        ("GPL-3.0-only", ["GPL-3.0-only"]),
        ("Apache-2.0 AND (GPL-3.0-only)", ["GPL-3.0-only"]),
        ("(Apache-2.0 AND MIT) AND CC-BY-NC-4.0", ["CC-BY-NC-4.0"]),
        ("MIT WITH LLVM-exception", ["MIT WITH LLVM-exception"]),  # admitted only for Apache
        ("Apache-2.0 WITH Classpath-exception-2.0", ["Apache-2.0 WITH Classpath-exception-2.0"]),
        ("GPL-2.0-only WITH LLVM-exception", ["GPL-2.0-only"]),
        ("apache-2.0", ["apache-2.0"]),  # ids are case-sensitive, as in qontinui
    ],
)
def test_what_is_not_admitted_is_named(expression: str, blocked: list[str]) -> None:
    assert spdx_blockers(expression) == blocked


@pytest.mark.parametrize(
    "expression",
    [
        "Apache-2.0 OR MIT",
        "MIT OR GPL-3.0-only",
        "(Apache-2.0 OR MIT) AND Zlib",
        "MIT AND (Zlib OR IJG)",
    ],
)
def test_an_or_is_refused_with_its_reason(expression: str) -> None:
    """qontinui admits a choice when one branch is admitted; an image's
    licence is the conjunction of what made it, so studio refuses a choice."""
    blocked = spdx_blockers(expression)
    assert OR_REFUSED in blocked
    assert "unsaid" in OR_REFUSED and "or later" in OR_REFUSED


@pytest.mark.parametrize(
    "expression",
    ["", "   ", "Apache-2.0 AND", "AND MIT", "(MIT", "MIT)", "MIT WITH", "MIT WITH (x)", "MIT; rm"],
)
def test_an_unparseable_expression_raises(expression: str) -> None:
    with pytest.raises(SpdxError):
        spdx_blockers(expression)


def test_a_facade_record_is_checked_with_the_parser() -> None:
    def record(spdx: str) -> dict[str, object]:
        return {
            "licence": {"spdx": spdx},
            "recipe": {"id": "hero", "hash": "d" * 64},
            "models": [{"id": "z", "weights": [{"path": "w", "sha256": "9" * 64}]}],
            "seed": 1,
        }

    assert provenance.facade_missing(record("(Apache-2.0 AND MIT) AND BSD-3-Clause")) == []
    assert provenance.facade_missing(record("Apache-2.0 WITH LLVM-exception")) == []
    [problem] = provenance.facade_missing(record("Apache-2.0 OR MIT"))
    assert "is not admitted" in problem and "unsaid" in problem
    [problem] = provenance.facade_missing(record("Apache-2.0 AND"))
    assert "unparseable" in problem
