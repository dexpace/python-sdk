# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Property-based tests pinning ``Status`` totality and code-equality.

Invariants covered:

- Totality over the HTTP range: every integer in 100..599 constructs a
  ``Status`` without raising and carries its integer value; every integer
  outside that range raises ``ValueError``.
- Equality is by numeric code: ``Status(code) == code`` and
  ``Status(a) == Status(b)`` iff ``a == b``; construction is idempotent.
- Band predicates agree with the arithmetic of the code's leading digit.

The example count and deadline are governed by the capped "ci" Hypothesis
profile registered in ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpace.sdk.core.http.response import Status

_in_range = st.integers(min_value=100, max_value=599)


@given(code=st.integers())
def test_totality_over_ints(code: int) -> None:
    """Any int in 100..599 constructs; anything else raises ``ValueError``."""
    if 100 <= code <= 599:
        status = Status(code)
        assert int(status) == code
        assert status == code
        assert Status(int(status)) == status  # construction is idempotent
    else:
        with pytest.raises(ValueError):
            Status(code)


@given(code=_in_range)
def test_band_classification_matches_arithmetic(code: int) -> None:
    """Each band predicate matches the arithmetic range of the code."""
    status = Status(code)
    assert status.is_informational == (100 <= code < 200)
    assert status.is_success == (200 <= code < 300)
    assert status.is_redirect == (300 <= code < 400)
    assert status.is_client_error == (400 <= code < 500)
    assert status.is_server_error == (500 <= code < 600)
    assert status.is_error == (status.is_client_error or status.is_server_error)


@given(left=_in_range, right=_in_range)
def test_equality_is_by_code(left: int, right: int) -> None:
    """Two ``Status`` values are equal iff their integer codes are equal."""
    assert (Status(left) == Status(right)) == (left == right)
