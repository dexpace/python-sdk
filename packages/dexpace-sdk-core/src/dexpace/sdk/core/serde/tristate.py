# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Three-valued optional type distinguishing "omitted" from "explicit null".

A field typed ``T | None`` cannot tell apart two wire states that matter for
merge-update (``PATCH``) APIs: a key that was *omitted entirely* versus a key
that was *sent as JSON ``null``*. Omitting ``name`` means "leave it unchanged";
sending ``name: null`` means "clear it".

``Tristate[T]`` is a sealed type with exactly three inhabitants:

- ``ABSENT`` — the key was omitted; on serialize, skip it.
- ``NULL`` — the key was present with value ``null``; on serialize, write ``null``.
- ``Present(value)`` — a real value; on serialize, write ``value``.

``fold`` forces callers to handle all three cases, so the absent-vs-null
distinction can never be silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeGuard, final

if TYPE_CHECKING:
    from collections.abc import Callable


@final
class _Absent:
    """Singleton type for the ``ABSENT`` sentinel — the key was omitted."""

    __slots__ = ()
    _instance: _Absent | None = None

    def __new__(cls) -> _Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __reduce__(self) -> str:
        return "ABSENT"


@final
class _Null:
    """Singleton type for the ``NULL`` sentinel — the key was an explicit null."""

    __slots__ = ()
    _instance: _Null | None = None

    def __new__(cls) -> _Null:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NULL"

    def __reduce__(self) -> str:
        return "NULL"


@final
@dataclass(frozen=True, slots=True)
class Present[T]:
    """A real, present value within a ``Tristate``.

    Attributes:
        value: The wrapped value. May itself be falsy (``0``, ``""``, ``[]``);
            presence is encoded by the wrapper, never by truthiness.
    """

    value: T


ABSENT: Final[_Absent] = _Absent()
"""The key was omitted entirely — serialize by skipping the key."""

NULL: Final[_Null] = _Null()
"""The key was present with an explicit null — serialize as ``null``."""


type Tristate[T] = _Absent | _Null | Present[T]
"""Three-valued optional: ``ABSENT`` | ``NULL`` | ``Present[T]``."""


def present[T](value: T) -> Present[T]:
    """Wrap a concrete value as ``Present``.

    Args:
        value: The value to wrap; falsy values are preserved as present.

    Returns:
        A ``Present`` holding ``value``.
    """
    return Present(value)


def of_optional[T](value: T | None) -> _Null | Present[T]:
    """Lift a plain optional into a ``Tristate``, mapping ``None`` to ``NULL``.

    Use this when the source can only distinguish "value" from "no value" (a
    bare ``T | None``) and ``None`` should mean an explicit null on the wire.
    The result can never be ``ABSENT``; omission must be expressed by the caller
    choosing ``ABSENT`` directly.

    Args:
        value: A value or ``None``.

    Returns:
        ``NULL`` if ``value is None``, otherwise ``Present(value)``.
    """
    if value is None:
        return NULL
    return Present(value)


def fold[T, R](
    state: Tristate[T],
    *,
    on_absent: Callable[[], R],
    on_null: Callable[[], R],
    on_present: Callable[[T], R],
) -> R:
    """Collapse a ``Tristate`` to a single value, handling every case.

    Exactly one branch runs. Because all three handlers are required, callers
    cannot silently forget the absent-vs-null distinction.

    Args:
        state: The tristate to inspect.
        on_absent: Called with no arguments when ``state`` is ``ABSENT``.
        on_null: Called with no arguments when ``state`` is ``NULL``.
        on_present: Called with the wrapped value when ``state`` is ``Present``.

    Returns:
        The result of whichever handler matched ``state``.
    """
    if isinstance(state, Present):
        return on_present(state.value)
    if state is NULL:
        return on_null()
    return on_absent()


def is_absent[T](state: Tristate[T]) -> TypeGuard[_Absent]:
    """Return whether ``state`` is the ``ABSENT`` sentinel."""
    return state is ABSENT


def is_null[T](state: Tristate[T]) -> TypeGuard[_Null]:
    """Return whether ``state`` is the ``NULL`` sentinel."""
    return state is NULL


def is_present[T](state: Tristate[T]) -> TypeGuard[Present[T]]:
    """Return whether ``state`` is a ``Present`` value."""
    return isinstance(state, Present)


__all__ = [
    "ABSENT",
    "NULL",
    "Present",
    "Tristate",
    "fold",
    "is_absent",
    "is_null",
    "is_present",
    "of_optional",
    "present",
]
