# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Unit of work in a distributed trace."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from .instrumentation_context import InstrumentationContext
    from .tracing_scope import TracingScope


class Span(ABC):
    """A single timed operation in a distributed trace.

    Captures attributes, errors, and timing for one operation; spans correlate
    across services through their shared trace context and may nest to model
    parent/child relationships.

    Most callers obtain a span from a `Tracer` and end it once the
    underlying operation completes. Operations that don't perform tracing
    receive the no-op `NOOP_SPAN` singleton.
    """

    @property
    @abstractmethod
    def is_recording(self) -> bool:
        """True when this span is sampled and recording events / timing."""

    @property
    @abstractmethod
    def context(self) -> InstrumentationContext:
        """Trace-related metadata for propagation and lookup."""

    @abstractmethod
    def set_attribute(self, key: str, value: Any) -> Self:
        """Attach a key/value attribute. Returns ``self`` for chaining."""

    @abstractmethod
    def set_error(self, error_type: str) -> Self:
        """Mark this span as having encountered an error of the given type."""

    def make_current(self) -> TracingScope:
        """Make this span the active span for the current execution context.

        The default activates this span in the ambient current-span
        ``contextvar`` and returns a `TracingScope` whose ``close()`` restores
        the previously active span — including when the scope body raises. Use
        as a context manager to guarantee cleanup. Backends may override to add
        their own book-keeping but should preserve the restore-on-close
        contract; the no-op span overrides this to return a shared do-nothing
        scope so the tracing-disabled path allocates nothing.
        """
        from .correlation import activate_span

        return activate_span(self)

    @abstractmethod
    def end(self, error: BaseException | None = None) -> None:
        """Finish the span.

        Pass ``error`` to record an exception as the cause. Calling `end`
        more than once, or on a non-recording span, is a documented no-op.
        """


__all__ = ["Span"]
