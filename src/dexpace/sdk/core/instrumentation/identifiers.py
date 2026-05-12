"""W3C Trace Context identifiers (TraceId, SpanId, TraceFlags, TraceState)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar


@dataclass(frozen=True)
class TraceId:
    """Identifier shared by every span in the same logical trace.

    The encoding (hex / decimal, length) depends on the :class:`TraceIdType` of
    the originating context.
    """

    value: str

    NOOP: ClassVar["TraceId"]


TraceId.NOOP = TraceId("0" * 32)  # type: ignore[misc]


@dataclass(frozen=True)
class SpanId:
    """Identifier of the current span within its parent trace.

    Rendered as a 16-character hex string per the W3C Trace Context spec.
    """

    value: str

    NOOP: ClassVar["SpanId"]


SpanId.NOOP = SpanId("0" * 16)  # type: ignore[misc]


@dataclass(frozen=True)
class TraceFlags:
    """W3C trace-flags — a two-character hex string carrying span-level bits."""

    value: str

    NOOP: ClassVar["TraceFlags"]


TraceFlags.NOOP = TraceFlags("00")  # type: ignore[misc]


@dataclass(frozen=True)
class TraceState:
    """W3C trace-state — vendor-specific key=value list propagated alongside context."""

    value: str

    NOOP: ClassVar["TraceState"]


TraceState.NOOP = TraceState("")  # type: ignore[misc]


class TraceIdType(Enum):
    """Encoding flavour of a :class:`TraceId`.

    Different backends expect different wire formats (W3C hex vs Datadog
    decimal); the type drives how the trace id is rendered for propagation.
    """

    NOOP = "noop"
    W3C = "w3c"
    DATADOG = "datadog"


__all__ = ["SpanId", "TraceFlags", "TraceId", "TraceIdType", "TraceState"]
