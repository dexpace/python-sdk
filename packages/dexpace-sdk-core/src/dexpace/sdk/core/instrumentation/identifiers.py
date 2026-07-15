# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""W3C Trace Context identifiers (TraceId, SpanId, TraceFlags, TraceState)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, Self
from uuid import uuid4

#: Bit width of a Datadog trace id (a 64-bit unsigned integer).
_DATADOG_BITS: Final = 64

#: Mask that projects a 128-bit UUID onto the low 64 bits for a Datadog id.
_DATADOG_MASK: Final = (1 << _DATADOG_BITS) - 1


def _draw_datadog_bits() -> int:
    """Draw a raw 64-bit value for a Datadog trace id.

    Derived from the low 64 bits of a `uuid.uuid4()` — a non-blocking,
    non-crypto source, appropriate for a correlation handle rather than a
    secret. Factored out so tests can force the reserved zero draw and prove
    the coercion in `_datadog_value`.

    Returns:
        An integer in ``[0, 2**64 - 1]``.
    """
    return uuid4().int & _DATADOG_MASK


def _datadog_value() -> str:
    """Render a non-zero 64-bit unsigned Datadog trace id as a decimal string.

    A zero draw is the reserved all-zero id, so it is coerced to ``1`` — the
    generator never yields the invalid sentinel.

    Returns:
        The trace id as a base-10 string.
    """
    return str(_draw_datadog_bits() or 1)


@dataclass(frozen=True, slots=True)
class TraceId:
    """Identifier shared by every span in the same logical trace.

    The encoding (hex / decimal, length) depends on the `TraceIdType` of
    the originating context. The all-zero `NOOP` value is the documented
    invalid sentinel; a generated trace id is never all-zero.
    """

    value: str

    NOOP: ClassVar[TraceId]

    @classmethod
    def generate(cls, id_type: TraceIdType | None = None) -> Self:
        """Return a fresh trace id in the requested flavour.

        The W3C flavour is sourced from `uuid.uuid4().hex`. UUID4 is a
        non-blocking, non-crypto generator, which is appropriate here: trace
        ids are correlation handles, not secrets (unlike nonces / credentials
        elsewhere, which use `secrets`). Neither generative flavour ever yields
        the reserved all-zero id — `uuid4` fixes the version nibble to ``'4'``
        so a W3C id is always non-zero, and a zero Datadog draw is coerced to a
        non-zero value.

        Args:
            id_type: The encoding flavour. ``None`` (the default) and
                `TraceIdType.W3C` yield a 128-bit value rendered as 32 lowercase
                hex characters. `TraceIdType.DATADOG` yields a 64-bit unsigned
                integer rendered as a decimal string. `TraceIdType.NOOP` yields
                the all-zero invalid sentinel.

        Returns:
            A newly generated `TraceId`.
        """
        if id_type is TraceIdType.NOOP:
            return cls(TraceId.NOOP.value)
        if id_type is TraceIdType.DATADOG:
            return cls(_datadog_value())
        return cls(uuid4().hex)

    @property
    def is_valid(self) -> bool:
        """True when this id is not the all-zero invalid sentinel."""
        return self != TraceId.NOOP


TraceId.NOOP = TraceId("0" * 32)


@dataclass(frozen=True, slots=True)
class SpanId:
    """Identifier of the current span within its parent trace.

    Rendered as a 16-character hex string per the W3C Trace Context spec. The
    all-zero `NOOP` value is the documented invalid sentinel; a generated span
    id is never all-zero.
    """

    value: str

    NOOP: ClassVar[SpanId]

    @classmethod
    def generate(cls) -> Self:
        """Return a fresh, valid W3C span id (16 lowercase hex characters).

        Sourced from the first 16 hex characters of `uuid.uuid4().hex` — a
        non-blocking, non-crypto source, appropriate for a correlation handle.
        The slice includes the version nibble ``'4'`` (hex index 12), so the
        result can never equal the all-zero `NOOP` sentinel by construction.

        Returns:
            A newly generated `SpanId`.
        """
        return cls(uuid4().hex[:16])

    @property
    def is_valid(self) -> bool:
        """True when this id is not the all-zero invalid sentinel."""
        return self != SpanId.NOOP


SpanId.NOOP = SpanId("0" * 16)


@dataclass(frozen=True, slots=True)
class TraceFlags:
    """W3C trace-flags — a two-character hex string carrying span-level bits."""

    value: str

    NOOP: ClassVar[TraceFlags]


TraceFlags.NOOP = TraceFlags("00")


@dataclass(frozen=True, slots=True)
class TraceState:
    """W3C trace-state — vendor-specific key=value list propagated alongside context."""

    value: str

    NOOP: ClassVar[TraceState]


TraceState.NOOP = TraceState("")


class TraceIdType(StrEnum):
    """Encoding flavour of a `TraceId`.

    Different backends expect different wire formats (W3C hex vs Datadog
    decimal); the type drives how the trace id is rendered for propagation.
    """

    NOOP = "noop"
    W3C = "w3c"
    DATADOG = "datadog"


__all__ = ["SpanId", "TraceFlags", "TraceId", "TraceIdType", "TraceState"]
