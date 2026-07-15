# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Property-based tests pinning ``Headers`` folding and equality algebra.

Invariants covered:

- Name-fold idempotence: names are stored in lower-case canonical form, and
  reconstructing a ``Headers`` from its own ``items()`` is the identity.
- Equality is exactly fold-equality: two ``Headers`` compare equal iff their
  normalised ``(name, values)`` data is equal, so header-name case never
  affects equality, and ``__hash__`` agrees with ``__eq__``.

The example count and deadline are governed by the capped "ci" Hypothesis
profile registered in ``tests/conftest.py``.
"""

from __future__ import annotations

from string import ascii_lowercase, ascii_uppercase

from hypothesis import given
from hypothesis import strategies as st

from dexpace.sdk.core.http.common import Headers

# RFC 7230 ``tchar`` set (both cases accepted on input; stored lower-cased).
_HEADER_NAME_CHARS = "!#$%&'*+-.^_`|~0123456789" + ascii_lowercase + ascii_uppercase

_header_names = st.text(alphabet=_HEADER_NAME_CHARS, min_size=1, max_size=20)
# Header values may hold any text except the CR / LF / NUL bytes the model
# rejects to prevent header injection.
_header_values = st.text(
    st.characters(codec="utf-8", exclude_characters="\r\n\x00"),
    max_size=40,
)
_entries = st.lists(st.tuples(_header_names, _header_values), max_size=12)


@given(entries=_entries)
def test_reconstruction_from_items_is_identity(entries: list[tuple[str, str]]) -> None:
    """Rebuilding a ``Headers`` from its own ``items()`` yields an equal value.

    Pins that normalisation (name lower-casing, value validation) is idempotent:
    feeding already-normalised data back through the constructor is a fixed
    point, and equal values hash equally.
    """
    headers = Headers(entries)
    rebuilt = Headers(list(headers.items()))
    assert rebuilt == headers
    assert hash(rebuilt) == hash(headers)


@given(entries=_entries)
def test_stored_names_are_lowercase(entries: list[tuple[str, str]]) -> None:
    """Every stored header name equals its own lower-casing (fold idempotence)."""
    headers = Headers(entries)
    for name in headers.names():
        assert name == name.lower()


@given(entries=_entries)
def test_name_case_does_not_affect_equality(entries: list[tuple[str, str]]) -> None:
    """Recasing input names produces an equal ``Headers`` (equality folds case)."""
    base = Headers(entries)
    upper = Headers([(name.upper(), value) for name, value in entries])
    swapped = Headers([(name.swapcase(), value) for name, value in entries])
    assert upper == base
    assert swapped == base
    assert hash(upper) == hash(base) == hash(swapped)


@given(left=_entries, right=_entries)
def test_equality_is_exactly_fold_equality(
    left: list[tuple[str, str]],
    right: list[tuple[str, str]],
) -> None:
    """Two ``Headers`` are equal iff their normalised ``items()`` match.

    Also pins the ``__eq__`` / ``__hash__`` contract: equal instances hash
    equally.
    """
    first = Headers(left)
    second = Headers(right)
    assert (first == second) == (first.items() == second.items())
    if first == second:
        assert hash(first) == hash(second)
