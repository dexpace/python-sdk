"""Base type for in-flight call contexts."""
from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from ...instrumentation import InstrumentationContext


class CallContext:
    """Base for in-flight call contexts.

    Subclasses (:class:`DispatchContext`, :class:`RequestContext`,
    :class:`ExchangeContext`) are frozen dataclasses carrying additional
    request / response data. Implements the context-manager protocol so
    callers can ``with`` a context to ensure the :data:`ContextStore` entry is
    evicted on exit.

    The shared :data:`ContextStore` is thread-safe; contexts for different
    trace ids can be promoted concurrently without external synchronisation.
    """

    instrumentation_context: InstrumentationContext  # supplied by subclasses

    def close(self) -> None:
        """Remove this context from :data:`ContextStore` (idempotent)."""
        from .context_store import ContextStore

        ContextStore.remove(self.instrumentation_context.trace_id.value)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = ["CallContext"]
