# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Base type for in-flight call contexts."""

from __future__ import annotations

import abc
from types import TracebackType
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from ...instrumentation import InstrumentationContext


class CallContext(abc.ABC):
    """Base for in-flight call contexts.

    Subclasses (`DispatchContext`, `RequestContext`,
    `ExchangeContext`) are frozen dataclasses carrying additional
    request / response data. Implements the context-manager protocol so
    callers can ``with`` a context to ensure the `ContextStore` entry is
    evicted on exit.

    The shared `ContextStore` is thread-safe; contexts can be promoted
    concurrently without external synchronisation, and calls that share a
    trace id stay isolated because each carries its own `store_key`.
    """

    instrumentation_context: InstrumentationContext  # supplied by subclasses
    store_key: str | None  # ContextStore key; None until the call is registered

    def close(self) -> None:
        """Remove this context from `ContextStore` (idempotent).

        Removes the entry under this call's `store_key`. The request and
        exchange tiers of one call share that key, so a single close clears
        both. A context that was never registered (``store_key is None``) is a
        no-op.
        """
        from .context_store import ContextStore

        if self.store_key is not None:
            ContextStore.remove(self.store_key)

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
