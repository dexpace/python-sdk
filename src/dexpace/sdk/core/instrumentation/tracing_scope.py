"""Lifecycle handle for an active span."""
from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType


class TracingScope(ABC):
    """Lifecycle handle for a span activated via :meth:`Span.make_current`.

    While the scope is open the associated span is the "current" span for the
    executing thread; closing the scope restores the previously active span.
    Use as a context manager (``with span.make_current() as scope: ...``) to
    guarantee cleanup on exceptions.
    """

    @abstractmethod
    def close(self) -> None:
        """Restore the previously active span. Idempotent."""

    def __enter__(self) -> TracingScope:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = ["TracingScope"]
