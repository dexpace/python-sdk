# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Metadata carried with every traced operation (W3C Trace Context)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .identifiers import SpanId, TraceFlags, TraceId, TraceIdType, TraceState

if TYPE_CHECKING:
    from .http_tracer import HttpTracerFactory
    from .span import Span


def _default_http_tracer_factory() -> HttpTracerFactory:
    """Return the shared no-op tracer factory.

    Imported lazily to avoid a circular import: ``noop`` imports this module to
    build the no-op context singleton.
    """
    from .noop import NOOP_HTTP_TRACER_FACTORY

    return NOOP_HTTP_TRACER_FACTORY


@dataclass(frozen=True, slots=True)
class InstrumentationContext:
    """Metadata carried with every traced operation.

    Compliant with the `W3C Trace Context spec
    <https://www.w3.org/TR/trace-context/>`_. Implementations propagate this
    context across service boundaries so spans on either side correlate into
    one logical trace.

    The shared no-op singleton :data:`NOOP_INSTRUMENTATION_CONTEXT` is used
    when tracing is disabled.

    ``http_tracer_factory`` mints a per-operation :class:`HttpTracer` for
    fine-grained request telemetry; it defaults to the no-op factory so callers
    that don't instrument pay nothing.
    """

    trace_id_type: TraceIdType
    trace_id: TraceId
    span_id: SpanId
    span: Span
    trace_flags: TraceFlags = TraceFlags.NOOP
    trace_state: TraceState = TraceState.NOOP
    is_remote: bool = False
    http_tracer_factory: HttpTracerFactory = field(default_factory=_default_http_tracer_factory)

    @property
    def is_valid(self) -> bool:
        """True when the context carries real (non-sentinel) trace identifiers."""
        return self.trace_id != TraceId.NOOP


__all__ = ["InstrumentationContext"]
