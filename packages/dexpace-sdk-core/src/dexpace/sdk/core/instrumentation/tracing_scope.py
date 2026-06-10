# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Lifecycle handle for an active span."""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self


class TracingScope(ABC):
    """Lifecycle handle for a span activated via `Span.make_current`.

    While the scope is open the associated span is the "current" span for the
    executing thread; closing the scope restores the previously active span.
    Use as a context manager (``with span.make_current() as scope: ...``) to
    guarantee cleanup on exceptions.
    """

    @abstractmethod
    def close(self) -> None:
        """Restore the previously active span. Idempotent."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = ["TracingScope"]
