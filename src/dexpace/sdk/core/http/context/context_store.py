"""Process-wide registry mapping a call's trace id to its current context."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_context import CallContext


class _ContextStore:
    """Process-wide registry mapping a call's trace id to its current
    :class:`CallContext`.

    Each promotion (:meth:`DispatchContext.to_request_context`,
    :meth:`RequestContext.to_exchange_context`) overwrites the entry so the
    latest snapshot is visible to downstream observers keyed by trace id.
    Entries are removed when callers honour :meth:`CallContext.close`.

    Thread-safe — every operation acquires the lock so the guarantee
    survives free-threaded CPython (PEP 703) and non-CPython runtimes that
    do not guarantee atomic dict ops.
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


#: Process-wide :class:`_ContextStore` singleton.
ContextStore = _ContextStore()

__all__ = ["ContextStore"]
