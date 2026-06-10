# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Process-wide registry mapping a call's trace id to its current context."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_context import CallContext


class _ContextStore:
    """Process-wide registry mapping a call's trace id to its current
    `CallContext`.

    Each promotion (`DispatchContext.to_request_context`,
    `RequestContext.to_exchange_context`) overwrites the entry so the
    latest snapshot is visible to downstream observers keyed by trace id.
    Entries are removed when callers honour `CallContext.close`.

    Thread-safe — every operation acquires the lock so the guarantee
    survives free-threaded CPython (PEP 703) and non-CPython runtimes that
    do not guarantee atomic dict ops.

    Two writers coexist deliberately. ``put`` is a *guarded install* that
    raises on a duplicate trace id; it is part of the public surface for
    callers that own a trace id exclusively and want a duplicate to surface
    as a programming error. ``set`` is the *unconditional overwrite* the
    promotion chain (``DispatchContext.to_request_context`` →
    ``RequestContext.to_exchange_context``) relies on, where the first
    promotion installs the entry and later promotions replace it in place.
    ``put`` therefore has no internal callers, but removing it would narrow
    the public, test-covered surface — so it stays.
    """

    __slots__ = ("_contexts", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[str, CallContext] = {}

    def get(self, trace_id: str) -> CallContext | None:
        """Return the context registered under ``trace_id``, or ``None``."""
        with self._lock:
            return self._contexts.get(trace_id)

    def put(self, trace_id: str, context: CallContext) -> None:
        """Register ``context`` under ``trace_id``; reject duplicate ids.

        Guarded install for callers that own a trace id exclusively: a
        re-registration is treated as a programming error. The promotion
        chain uses `set` instead, which overwrites unconditionally.

        Args:
            trace_id: The key to register ``context`` under.
            context: The context to store.

        Raises:
            ValueError: if ``trace_id`` is already registered.
        """
        with self._lock:
            if trace_id in self._contexts:
                raise ValueError(f"trace_id already registered: {trace_id!r}")
            self._contexts[trace_id] = context

    def set(self, trace_id: str, context: CallContext) -> None:
        """Unconditionally store ``context`` under ``trace_id``.

        Used by the promotion chain, where the first promotion installs the
        entry and later promotions overwrite it. Holds the lock so the
        guarantee survives free-threaded CPython (PEP 703) and non-CPython
        runtimes that don't guarantee atomic dict writes.
        """
        with self._lock:
            self._contexts[trace_id] = context

    def remove(self, trace_id: str) -> None:
        """Remove the entry under ``trace_id``. No-op if absent."""
        with self._lock:
            self._contexts.pop(trace_id, None)


#: Process-wide `_ContextStore` singleton.
ContextStore = _ContextStore()

__all__ = ["ContextStore"]
