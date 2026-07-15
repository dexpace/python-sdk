# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Process-wide, bounded registry mapping a call's registration key to its
context."""

from __future__ import annotations

import itertools
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_context import CallContext

#: Default upper bound on live entries in a `_ContextStore`.
#:
#: Entries are added on promotion and removed on `CallContext.close`, so a
#: well-behaved process holds only as many entries as it has in-flight calls —
#: far below this bound. The cap is a safety valve against callers that leak by
#: never closing: it keeps a forgotten entry from growing the map without
#: limit, at the cost of evicting the oldest entry once the bound is crossed.
_DEFAULT_MAX_ENTRIES = 100_000


class _ContextStore:
    """Process-wide, bounded registry mapping a call's registration key to its
    current `CallContext`.

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
    callers honour `CallContext.close`, and are held by strong reference until
    then — eviction is explicit, never a weak-reference side effect of garbage
    collection.

    Bounded — the map holds at most ``max_entries`` live entries. Each insert
    (`put` / `set`) drains the oldest excess entries once the bound is crossed,
    closing every evicted context. The close side effect runs *after* the
    store lock is released, because closing a context re-enters the store
    (`CallContext.close` calls `remove`); running it under the lock would
    deadlock the non-reentrant lock.

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

    Two removers coexist for the same reason. ``remove`` is an *unconditional*
    pop-by-key: a call's request and exchange tiers share one key, so a single
    ``remove`` must clear whichever tier currently occupies the slot even
    though `close` is invoked on the (distinct) request-tier object. Making it
    identity-conditional would leak the promoted exchange tier. ``remove_if``
    is the *identity-conditional* counterpart for callers that must not clobber
    a successor: it evicts only when the stored entry *is* the given context,
    so an equal-but-distinct object at the same key leaves the live entry
    untouched.
    """

    __slots__ = ("_contexts", "_counter", "_lock", "_max_entries")

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        """Create an empty store bounded at ``max_entries`` live entries.

        Args:
            max_entries: Maximum number of live entries to retain. Once an
                insert crosses this bound, the oldest excess entries are
                drained and closed.

        Raises:
            ValueError: if ``max_entries`` is less than one.
        """
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._lock = threading.Lock()
        self._contexts: dict[str, CallContext] = {}
        self._counter = itertools.count()
        self._max_entries = max_entries

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
            evicted = self._drain_to_cap()
        self._close_evicted(evicted)

    def set(self, key: str, context: CallContext) -> None:
        """Unconditionally store ``context`` under ``key``.

        Used by the promotion chain, where the first promotion installs the
        entry and later promotions overwrite it. Holds the lock so the
        guarantee survives free-threaded CPython (PEP 703) and non-CPython
        runtimes that don't guarantee atomic dict writes.
        """
        with self._lock:
            self._contexts[key] = context
            evicted = self._drain_to_cap()
        self._close_evicted(evicted)

    def remove(self, key: str) -> None:
        """Remove the entry under ``key`` unconditionally. No-op if absent.

        A call's request and exchange tiers share one key, so removing that key
        clears whichever tier currently occupies the slot in a single call —
        the eviction `CallContext.close` relies on, even though it is invoked
        on the request-tier object after promotion replaced it with the
        exchange tier. Use `remove_if` when a successor at the same key must be
        preserved.
        """
        with self._lock:
            self._contexts.pop(key, None)

    def remove_if(self, key: str, context: CallContext) -> bool:
        """Remove the entry under ``key`` only if it *is* ``context``.

        Identity-conditional via ``is``: an equal-but-distinct context
        registered at the same key is left in place, so a stale predecessor
        cannot evict the successor that replaced it. This is the safe
        counterpart to the unconditional `remove` for callers that own a
        specific object rather than a whole key.

        Args:
            key: The key whose entry to consider.
            context: The context that must currently occupy ``key`` for the
                removal to happen.

        Returns:
            ``True`` if this call removed the entry; ``False`` if ``key`` was
            absent or held a different object.
        """
        with self._lock:
            if self._contexts.get(key) is context:
                del self._contexts[key]
                return True
            return False

    def _drain_to_cap(self) -> list[CallContext]:
        """Pop oldest entries until the map is at or below its cap.

        The caller must hold `self._lock`. Returns the evicted contexts so the
        caller can run their close side effects *after* releasing the lock: an
        evicted context's `close` re-enters the store, which would deadlock a
        re-acquire of the non-reentrant lock.

        Returns:
            The evicted contexts, oldest first (empty if the map is within cap).
        """
        evicted: list[CallContext] = []
        while len(self._contexts) > self._max_entries:
            oldest_key = next(iter(self._contexts))
            evicted.append(self._contexts.pop(oldest_key))
        return evicted

    @staticmethod
    def _close_evicted(evicted: list[CallContext]) -> None:
        """Run each evicted context's `close` side effect outside the lock.

        Called after the store lock is released so a `close` that re-enters the
        store (its own `remove`, already a no-op here) does not deadlock.
        """
        for context in evicted:
            context.close()

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
