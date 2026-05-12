"""No-op singletons for tracing-disabled code paths."""
from __future__ import annotations

from typing import Any, Final

from .identifiers import SpanId, TraceFlags, TraceId, TraceIdType, TraceState
from .instrumentation_context import InstrumentationContext
from .span import Span
from .tracer import Tracer
from .tracing_scope import TracingScope


class _NoopScope(TracingScope):
    """Reused across every :meth:`Span.make_current` call so the no-op path
    allocates nothing."""

    def close(self) -> None:
        return None


_NOOP_SCOPE = _NoopScope()


class _NoopSpan(Span):
    """No-op :class:`Span` — records nothing, returns ``self`` from every mutator.

    Use the shared :data:`NOOP_SPAN` singleton; this class is not part of the
    public API.
    """

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def context(self) -> InstrumentationContext:
        return NOOP_INSTRUMENTATION_CONTEXT

    def set_attribute(self, key: str, value: Any) -> _NoopSpan:
        return self

    def set_error(self, error_type: str) -> _NoopSpan:
        return self

    def make_current(self) -> TracingScope:
        return _NOOP_SCOPE

    def end(self, error: BaseException | None = None) -> None:
        return None


#: Shared no-op :class:`Span` singleton. Use when tracing is disabled.
NOOP_SPAN: Final[Span] = _NoopSpan()


#: Shared no-op :class:`InstrumentationContext` singleton.
NOOP_INSTRUMENTATION_CONTEXT: Final[InstrumentationContext] = InstrumentationContext(
    trace_id_type=TraceIdType.NOOP,
    trace_id=TraceId.NOOP,
    span_id=SpanId.NOOP,
    span=NOOP_SPAN,
    trace_flags=TraceFlags.NOOP,
    trace_state=TraceState.NOOP,
    is_remote=False,
)


class _NoopTracer(Tracer):
    """No-op :class:`Tracer` — every :meth:`start_span` returns :data:`NOOP_SPAN`."""

    def start_span(
        self,
        name: str,
        parent: InstrumentationContext | None = None,
    ) -> Span:
        return NOOP_SPAN


#: Shared no-op :class:`Tracer` singleton.
NOOP_TRACER: Final[Tracer] = _NoopTracer()


__all__ = ["NOOP_INSTRUMENTATION_CONTEXT", "NOOP_SPAN", "NOOP_TRACER"]
