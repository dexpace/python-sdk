# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Process-wide registry mapping a call's registration key to its context."""

from __future__ import annotations

import itertools
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_context import CallContext


class _ContextStore:
    """Process-wide registry mapping a call's registration key to its current
    `CallContext`.

    Keys are minted by `new_key` as ``{trace_id}:{span_id}:{n}``, where ``n``
    is drawn from a process-wide counter. The trace id alone is *not* a unique
    key: a single traced operation can fan out into several concurrent calls
    that share a trace id (and span id), so a trace-id-only key would let one
    call's registration overwrite another's, or let one call's `close` evict a
    sibling's live context. The counter segment gives every call its own slot.
    The trace id stays the leading, colon-delimited segment so a prefix scan by
    trace id (`contexts_for_trace`) still finds every live context for a call.

    Each promotion (`DispatchContext.to_request_context`,
    `RequestContext.to_exchange_context`) reuses the call's key so the latest
    snapshot overwrites the previous tier in place. Entries are removed when
    callers honour `CallContext.close`.

    Thread-safe — every operation, including each counter increment, acquires
    the lock so the guarantee survives free-threaded CPython (PEP 703) and
    non-CPython runtimes that do not guarantee atomic dict ops or an atomic
    `next` on `itertools.count`.

    Two writers coexist deliberately. ``put`` is a *guarded install* that
    raises on a duplicate key; it is part of the public surface for callers
    that own a key exclusively and want a duplicate to surface as a programming
    error. ``set`` is the *unconditional overwrite* the promotion chain
    (``DispatchContext.to_request_context`` →
    ``RequestContext.to_exchange_context``) relies on, where the first
    promotion installs the entry and later promotions replace it in place.
    ``put`` therefore has no internal callers, but removing it would narrow the
    public, test-covered surface — so it stays.
    """

    __slots__ = ("_contexts", "_counter", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[str, CallContext] = {}
        self._counter = itertools.count()

    def new_key(self, trace_id: str, span_id: str) -> str:
        """Mint a process-unique registration key for a single call.

        The key is ``{trace_id}:{span_id}:{n}`` where ``n`` comes from a
        process-wide counter drawn under the lock. Two concurrent calls that
        share a trace id and span id still receive distinct keys, so neither
        overwrites nor evicts the other. The trace id and span id are W3C hex
        identifiers containing no colon, so the leading ``{trace_id}:`` segment
        stays an unambiguous prefix for `contexts_for_trace`.

        Args:
            trace_id: The call's trace id; becomes the key's leading segment.
            span_id: The call's span id; the key's middle segment.

        Returns:
            A registration key unique across the process for this call.
        """
        with self._lock:
            n = next(self._counter)
        return f"{trace_id}:{span_id}:{n}"

    def get(self, key: str) -> CallContext | None:
        """Return the context registered under ``key``, or ``None``."""
        with self._lock:
            return self._contexts.get(key)

    def put(self, key: str, context: CallContext) -> None:
        """Register ``context`` under ``key``; reject a duplicate key.

        Guarded install for callers that own a key exclusively: a
        re-registration is treated as a programming error. The promotion chain
        uses `set` instead, which overwrites unconditionally.

        Args:
            key: The key to register ``context`` under.
            context: The context to store.

        Raises:
            ValueError: if ``key`` is already registered.
        """
        with self._lock:
            if key in self._contexts:
                raise ValueError(f"key already registered: {key!r}")
            self._contexts[key] = context

    def set(self, key: str, context: CallContext) -> None:
        """Unconditionally store ``context`` under ``key``.

        Used by the promotion chain, where the first promotion installs the
        entry and later promotions overwrite it. Holds the lock so the
        guarantee survives free-threaded CPython (PEP 703) and non-CPython
        runtimes that don't guarantee atomic dict writes.
        """
        with self._lock:
            self._contexts[key] = context

    def remove(self, key: str) -> None:
        """Remove the entry under ``key``. No-op if absent.

        A call's request and exchange tiers share one key, so removing that key
        clears both tiers in a single call — the eviction `CallContext.close`
        relies on.
        """
        with self._lock:
            self._contexts.pop(key, None)

    def contexts_for_trace(self, trace_id: str) -> list[CallContext]:
        """Return every live context whose key belongs to ``trace_id``.

        Scans for keys with the leading ``{trace_id}:`` segment, so a fan-out
        that produced several concurrent calls under one trace id yields all of
        their live contexts. The trailing colon prevents a shorter trace id
        from matching a longer one that merely shares its opening characters.

        Args:
            trace_id: The trace id to scan for.

        Returns:
            A snapshot list of the matching live contexts, in insertion order.
        """
        prefix = f"{trace_id}:"
        with self._lock:
            return [ctx for key, ctx in self._contexts.items() if key.startswith(prefix)]


#: Process-wide `_ContextStore` singleton.
ContextStore = _ContextStore()

__all__ = ["ContextStore"]
