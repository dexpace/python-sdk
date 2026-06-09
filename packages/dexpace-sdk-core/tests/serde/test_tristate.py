# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the three-valued ``Tristate`` optional type."""

from __future__ import annotations

import copy
import pickle

import pytest

from dexpace.sdk.core.serde import (
    ABSENT,
    NULL,
    Present,
    Tristate,
    fold,
    is_absent,
    is_null,
    is_present,
    of_optional,
    present,
)


def test_present_wraps_value() -> None:
    wrapped = present("x")
    assert isinstance(wrapped, Present)
    assert wrapped.value == "x"


def test_present_preserves_falsy_values() -> None:
    falsy_values: tuple[object, ...] = (0, "", [], False, 0.0)
    for falsy in falsy_values:
        wrapped = present(falsy)
        assert isinstance(wrapped, Present)
        assert wrapped.value == falsy
        assert is_present(wrapped)


def test_present_is_frozen() -> None:
    wrapped = present(1)
    with pytest.raises(AttributeError):
        wrapped.value = 2  # type: ignore[misc]  # frozen-dataclass guard under test


def test_present_equality_and_hash() -> None:
    assert present(1) == present(1)
    assert present(1) != present(2)
    assert hash(present(1)) == hash(Present(1))


def test_absent_and_null_are_singletons() -> None:
    assert ABSENT is ABSENT
    assert NULL is NULL
    assert ABSENT is not NULL  # type: ignore[comparison-overlap]  # distinct singletons
    assert type(ABSENT)() is ABSENT
    assert type(NULL)() is NULL


def test_sentinel_reprs() -> None:
    assert repr(ABSENT) == "ABSENT"
    assert repr(NULL) == "NULL"


def test_singletons_survive_copy() -> None:
    assert copy.copy(ABSENT) is ABSENT
    assert copy.deepcopy(ABSENT) is ABSENT
    assert copy.copy(NULL) is NULL
    assert copy.deepcopy(NULL) is NULL


def test_singletons_survive_pickle() -> None:
    assert pickle.loads(pickle.dumps(ABSENT)) is ABSENT
    assert pickle.loads(pickle.dumps(NULL)) is NULL


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, NULL),
        (0, Present(0)),
        ("", Present("")),
        ("hello", Present("hello")),
        ([], Present([])),
    ],
    ids=["none-to-null", "zero", "empty-str", "str", "empty-list"],
)
def test_of_optional_maps_none_to_null(value: object, expected: Tristate[object]) -> None:
    assert of_optional(value) == expected


def test_of_optional_never_returns_absent() -> None:
    # of_optional never yields ABSENT; the identity check can't statically overlap.
    assert of_optional(None) is not ABSENT  # type: ignore[comparison-overlap]
    assert of_optional("x") is not ABSENT  # type: ignore[comparison-overlap]


def test_guards_are_mutually_exclusive() -> None:
    cases: list[Tristate[int]] = [ABSENT, NULL, present(7)]
    for state in cases:
        flags = [is_absent(state), is_null(state), is_present(state)]
        assert sum(flags) == 1


def test_is_absent() -> None:
    assert is_absent(ABSENT)
    assert not is_absent(NULL)
    assert not is_absent(present(1))


def test_is_null() -> None:
    assert is_null(NULL)
    assert not is_null(ABSENT)
    assert not is_null(present(1))


def test_is_present() -> None:
    assert is_present(present(1))
    assert not is_present(ABSENT)
    assert not is_present(NULL)


def test_fold_dispatches_to_present() -> None:
    result = fold(
        present(10),
        on_absent=lambda: "absent",
        on_null=lambda: "null",
        on_present=lambda v: f"present:{v}",
    )
    assert result == "present:10"


def test_fold_dispatches_to_null() -> None:
    result = fold(
        NULL,
        on_absent=lambda: "absent",
        on_null=lambda: "null",
        on_present=lambda v: f"present:{v}",
    )
    assert result == "null"


def test_fold_dispatches_to_absent() -> None:
    result = fold(
        ABSENT,
        on_absent=lambda: "absent",
        on_null=lambda: "null",
        on_present=lambda v: f"present:{v}",
    )
    assert result == "absent"


def test_fold_runs_exactly_one_branch() -> None:
    calls: list[str] = []
    fold(
        present("v"),
        on_absent=lambda: calls.append("absent"),
        on_null=lambda: calls.append("null"),
        on_present=lambda _v: calls.append("present"),
    )
    assert calls == ["present"]


def test_fold_present_passes_falsy_value() -> None:
    result = fold(
        present(0),
        on_absent=lambda: -1,
        on_null=lambda: -2,
        on_present=lambda v: v,
    )
    assert result == 0


def test_fold_is_exhaustive_over_all_inhabitants() -> None:
    def describe(state: Tristate[int]) -> str:
        return fold(
            state,
            on_absent=lambda: "absent",
            on_null=lambda: "null",
            on_present=lambda v: f"present:{v}",
        )

    assert describe(ABSENT) == "absent"
    assert describe(NULL) == "null"
    assert describe(present(3)) == "present:3"


def test_serialize_semantics_via_fold() -> None:
    """ABSENT omits the key, NULL writes null, Present writes the value."""

    def encode(field: str, state: Tristate[object]) -> dict[str, object]:
        return fold(
            state,
            on_absent=lambda: {},
            on_null=lambda: {field: None},
            on_present=lambda v: {field: v},
        )

    assert encode("name", ABSENT) == {}
    assert encode("name", NULL) == {"name": None}
    assert encode("name", present("Ada")) == {"name": "Ada"}
