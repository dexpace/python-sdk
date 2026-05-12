"""Metadata carried with every traced operation (W3C Trace Context)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .identifiers import SpanId, TraceFlags, TraceId, TraceIdType, TraceState

if TYPE_CHECKING:
    from .span import Span


@dataclass(frozen=True)
class InstrumentationContext:
    """Metadata carried with every traced operation.

    Compliant with the `W3C Trace Context spec
    <https://www.w3.org/TR/trace-context/>`_. Implementations propagate this
    context across service boundaries so spans on either side correlate into
    one logical trace.

    The shared no-op singleton :data:`NOOP_INSTRUMENTATION_CONTEXT` is used
    when tracing is disabled.
    """

    trace_id_type: TraceIdType
    trace_id: TraceId
    span_id: SpanId
    span: "Span"
    trace_flags: TraceFlags = TraceFlags.NOOP
    trace_state: TraceState = TraceState.NOOP
    is_remote: bool = False

    @property
    def is_valid(self) -> bool:
        """True when the context carries real (non-sentinel) trace identifiers."""
        return self.trace_id != TraceId.NOOP


__all__ = ["InstrumentationContext"]
