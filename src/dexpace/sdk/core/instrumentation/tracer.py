"""Factory for :class:`Span` instances."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .instrumentation_context import InstrumentationContext
    from .span import Span


class Tracer(ABC):
    """Backend-specific span factory.

    Implementations integrate with OpenTelemetry, Datadog, etc. The SDK ships
    only the contract and a no-op default; consuming applications install a
    real tracer per their telemetry stack.
    """

    @abstractmethod
    def start_span(
        self,
        name: str,
        parent: InstrumentationContext | None = None,
    ) -> Span:
        """Start and return a new span.

        ``parent`` carries the trace identifiers to inherit; when ``None``, the
        implementation may start a new trace or use the currently-active span
        as parent (depending on backend conventions).
        """


__all__ = ["Tracer"]
