"""SPDX licence expressions, admitted exactly as qontinui's image licence
manifest admits them (decision record ``image-generation-weights-are-
permissively-licensed``, a5ec7670 v3).

A port of qontinui ``docker/imagegen/qontinui_imagegen/manifest.py``
(``_tokenize_spdx``, ``_SpdxParser``, ``_blockers``, ``ADMITTED_SPDX``,
``ADMITTED_EXCEPTIONS``) at qontinui ``a9dbb1a69ee03b3887b29defa21781c2ae1cb38e``:
the same tokens, the same precedence (OR loosest, then AND, then WITH), the
same ids and the one admitted ``WITH`` exception. ``tests/test_spdx.py`` pins
the id set and the exception map to those values, so a change to qontinui's
admitted set is a deliberate change here too.

One deliberate difference: an ``OR`` anywhere is refused. qontinui admits a
choice when one branch is admitted (a component offered under several
licences); the licence a generated IMAGE carries is the conjunction of what
made it, and a facade stamping an OR would leave which licence governs the
pixels unsaid — so studio refuses it with that reason rather than guess.
"""

from __future__ import annotations

import re
from typing import Any

ADMITTED_SPDX: frozenset[str] = frozenset(
    {
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
)
ADMITTED_EXCEPTIONS: dict[str, frozenset[str]] = {"Apache-2.0": frozenset({"LLVM-exception"})}

OR_REFUSED = (
    "an OR leaves which licence governs the image unsaid; a facade result names the "
    "conjunction of what made it"
)

_TOKEN = re.compile(r"\(|\)|[A-Za-z0-9][A-Za-z0-9.:-]*\+?")
_OPERATORS = frozenset({"AND", "OR", "WITH"})
_Node = tuple[Any, ...]


class SpdxError(ValueError):
    """The expression does not parse."""


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            position += 1
            continue
        match = _TOKEN.match(expression, position)
        if match is None:
            raise SpdxError(f"unexpected character {expression[position]!r} at {position}")
        tokens.append(match.group(0))
        position = match.end()
    if not tokens:
        raise SpdxError("empty licence expression")
    return tokens


class _Parser:
    """Recursive descent: OR binds loosest, then AND, then WITH."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._index = 0

    def parse(self) -> _Node:
        node = self._or()
        if self._peek() is not None:
            raise SpdxError(f"unexpected {self._peek()!r} after a complete expression")
        return node

    def _peek(self) -> str | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take(self) -> str | None:
        token = self._peek()
        self._index += 1
        return token

    def _or(self) -> _Node:
        branches = [self._and()]
        while self._peek() == "OR":
            self._take()
            branches.append(self._and())
        return branches[0] if len(branches) == 1 else ("OR", branches)

    def _and(self) -> _Node:
        terms = [self._term()]
        while self._peek() == "AND":
            self._take()
            terms.append(self._term())
        return terms[0] if len(terms) == 1 else ("AND", terms)

    def _term(self) -> _Node:
        token = self._take()
        if token is None:
            raise SpdxError("the expression ends where a licence was expected")
        if token == "(":
            inner = self._or()
            if self._take() != ")":
                raise SpdxError("unbalanced parenthesis")
            return inner
        if token == ")" or token in _OPERATORS:
            raise SpdxError(f"unexpected {token!r} where a licence was expected")
        leaf: _Node = ("ID", token)
        if self._peek() == "WITH":
            self._take()
            exception = self._take()
            if exception is None or exception in ("(", ")") or exception in _OPERATORS:
                raise SpdxError("WITH must be followed by an exception identifier")
            leaf = ("WITH", leaf, exception)
        return leaf


def _base_id(identifier: str) -> str:
    return identifier[:-1] if identifier.endswith("+") else identifier


def _blockers(node: _Node) -> list[str]:
    kind = node[0]
    if kind == "ID":
        return [] if _base_id(node[1]) in ADMITTED_SPDX else [node[1]]
    if kind == "WITH":
        base = _base_id(node[1][1])
        if base not in ADMITTED_SPDX:
            return [node[1][1]]
        if node[2] not in ADMITTED_EXCEPTIONS.get(base, frozenset()):
            return [f"{node[1][1]} WITH {node[2]}"]
        return []
    if kind == "OR":
        return [OR_REFUSED]
    return [blocked for child in node[1] for blocked in _blockers(child)]


def spdx_blockers(expression: str) -> list[str]:
    """What keeps ``expression`` from being admitted for an image; ``[]``
    means admitted. ``AND`` needs every operand admitted; ``X WITH e`` needs
    ``X`` admitted and ``e`` admitted for ``X``; ``OR`` is refused. Raises
    :class:`SpdxError` for an expression that does not parse."""
    return _blockers(_Parser(_tokenize(expression)).parse())
