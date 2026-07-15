# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Hardening tests for `_ContextStore`.

Certifies the store's safety properties against fresh instances so each test
controls the bound and starts from an empty map:

* reject-on-duplicate (`put`) loses deterministically;
* identity-conditional removal (`remove_if`) never cross-evicts an
  equal-but-distinct successor, while the unconditional shared-key `remove`
  the promotion chain's `close` relies on still clears a promoted call;
* post-insert drain-to-cap keeps the map bounded, evicting oldest-first, and
  closing an evicted entry never deadlocks even when its close re-enters the
  store;
* entries are held by strong reference (no weak-ref eviction).
"""

from __future__ import annotations

import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import (
    CallContext,
    ContextStore,
    DispatchContext,
)
from dexpace.sdk.core.http.context.context_store import _ContextStore
from dexpace.sdk.core.http.context.request_context import RequestContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN


def _instrumentation(trace_id_value: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace_id_value),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


def _response(request: Request) -> Response:
    return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


def _request_context(store_key: str) -> RequestContext:
    """Build a `RequestContext` directly, without touching any store."""
    return RequestContext(
        instrumentation_context=_instrumentation("0" * 16 + "1"),
        request=_request(),
        store_key=store_key,
    )


class _StoreCoupledContext(CallContext):
    """Minimal `CallContext` whose `close` re-enters a specific store.

    The eviction side effect deliberately calls back into the same store, so
    the drain path is exercised against the deadlock trap: if drain held its
    lock while running this `close`, the re-entrant `remove` would deadlock.
    """

    def __init__(self, store: _ContextStore, key: str) -> None:
        self._store = store
        self.store_key = key
        self.closed = False

    def close(self) -> None:
        # Re-enter the *same* store from the eviction side effect.
        key = self.store_key
        if key is not None:
            self._store.remove(key)
        self.closed = True


# --- reject-on-duplicate ------------------------------------------------------


@pytest.mark.req("CTX-8")
def test_put_rejects_duplicate_key_deterministically() -> None:
    """A second `put` under a registered key loses with a `ValueError`."""
    store = _ContextStore()
    ctx = _request_context("k")
    store.put("k", ctx)
    with pytest.raises(ValueError, match="already registered"):
        store.put("k", _request_context("k"))
    # The original entry survives the rejected duplicate.
    assert store.get("k") is ctx


# --- identity-conditional removal vs shared-key close -------------------------


def test_identity_conditional_remove_coexists_with_shared_key_close() -> None:
    """`remove_if` is identity-scoped; `close` stays unconditional shared-key.

    Both properties must hold together:

    * `remove_if(key, ctx)` evicts only when the stored entry *is* ``ctx``, so
      an equal-but-distinct twin cannot cross-evict the live entry.
    * The unconditional `remove` behind `CallContext.close` still clears a
      promoted call whose request and exchange tiers share one key, even though
      `close` runs on the request-tier object while the exchange-tier object is
      what occupies the slot.
    """
    # Identity-conditional remove_if on a private instance.
    store = _ContextStore()
    key = store.new_key("0" * 32, "0" * 16)
    real = _request_context(key)
    twin = _request_context(key)
    assert real == twin and real is not twin  # equal but distinct
    store.set(key, real)
    assert store.remove_if(key, twin) is False  # twin must not evict real
    assert store.get(key) is real
    assert store.remove_if(key, real) is True  # the real object evicts
    assert store.get(key) is None
    assert store.remove_if(key, real) is False  # absent key -> False

    # Shared-key unconditional close still clears the promoted exchange.
    instr = _instrumentation("0" * 16 + "d")
    request_ctx = DispatchContext(instrumentation_context=instr).to_request_context(_request())
    shared_key = request_ctx.store_key
    assert shared_key is not None
    exchange = request_ctx.to_exchange_context(_response(request_ctx.request))
    # The exchange tier (a distinct object) now occupies the shared key, yet
    # close is invoked on the request-tier object.
    assert ContextStore.get(shared_key) is exchange
    request_ctx.close()  # unconditional remove, not identity-conditional
    assert ContextStore.get(shared_key) is None  # cleared despite identity mismatch


@pytest.mark.req("CTX-18", "CTX-8")
def test_remove_is_unconditional_pop_by_key() -> None:
    """`remove` clears a key regardless of which object occupies it."""
    store = _ContextStore()
    original = _request_context("k")
    successor = _request_context("k")
    store.set("k", original)
    store.set("k", successor)  # overwrite in place, same key
    store.remove("k")  # unconditional -- clears whatever is there
    assert store.get("k") is None


# --- strong references --------------------------------------------------------


@pytest.mark.req("CTX-19")
def test_store_holds_strong_reference() -> None:
    """A stored context is retained by strong reference, not a weak one."""
    store = _ContextStore()
    ctx = _request_context("k")
    ref = weakref.ref(ctx)
    store.set("k", ctx)
    del ctx
    gc.collect()
    assert ref() is not None  # survived GC -> the store holds a strong ref
    assert store.get("k") is ref()


# --- post-insert drain-to-cap -------------------------------------------------


@pytest.mark.req("CTX-11")
def test_nonpositive_cap_is_rejected() -> None:
    """A store must be bounded by at least one entry."""
    with pytest.raises(ValueError, match="max_entries"):
        _ContextStore(max_entries=0)


@pytest.mark.req("CTX-11", "CTX-13", "XCUT-14")
def test_set_drains_to_cap_oldest_first() -> None:
    """Inserting past the cap evicts the oldest entries first."""
    store = _ContextStore(max_entries=3)
    contexts = {f"k{i}": _StoreCoupledContext(store, f"k{i}") for i in range(5)}
    for key, ctx in contexts.items():
        store.set(key, ctx)
    # Only the three newest keys remain; the two oldest were drained.
    assert store.get("k0") is None
    assert store.get("k1") is None
    assert store.get("k2") is contexts["k2"]
    assert store.get("k3") is contexts["k3"]
    assert store.get("k4") is contexts["k4"]
    # The evicted entries had their close side effect run; the survivor did not.
    assert contexts["k0"].closed and contexts["k1"].closed
    assert not contexts["k4"].closed


@pytest.mark.req("CTX-11", "XCUT-14")
def test_put_also_drains_to_cap() -> None:
    """`put`, like `set`, enforces the cap after inserting."""
    store = _ContextStore(max_entries=2)
    a, b, c = (_StoreCoupledContext(store, k) for k in ("a", "b", "c"))
    store.put("a", a)
    store.put("b", b)
    store.put("c", c)
    assert store.get("a") is None  # oldest drained
    assert store.get("b") is b
    assert store.get("c") is c
    assert a.closed


@pytest.mark.req("CTX-12", "NFR-11", "XCUT-11", "XCUT-14")
def test_cap_drain_converges_under_concurrent_inserts() -> None:
    """Concurrent inserts converge to exactly the cap, with no lost lock."""
    cap = 64
    store = _ContextStore(max_entries=cap)
    workers = 8
    per_worker = 500

    def worker(base: int) -> None:
        for i in range(per_worker):
            key = f"{base}:{i}"
            store.set(key, _StoreCoupledContext(store, key))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, b) for b in range(workers)]
        for future in futures:
            future.result(timeout=30)

    # Every insert drains back to the cap, so the final size is exactly the cap.
    assert len(store._contexts) == cap


@pytest.mark.req("NFR-11")
def test_drain_close_side_effect_reenters_store_without_deadlock() -> None:
    """An evicted entry's `close` may re-enter the store without deadlocking.

    Drain must release the store lock before running an eviction side effect,
    because that side effect can call back into the store. The insert that
    triggers the drain runs in a worker thread so a deadlock surfaces as a
    timeout rather than hanging the suite.
    """
    store = _ContextStore(max_entries=1)
    victim = _StoreCoupledContext(store, "victim")
    store.set("victim", victim)  # at cap, no drain yet
    survivor = _StoreCoupledContext(store, "survivor")

    done = threading.Event()

    def insert() -> None:
        store.set("survivor", survivor)  # overflow -> drain evicts + closes victim
        done.set()

    thread = threading.Thread(target=insert)
    thread.start()
    assert done.wait(timeout=5.0), "drain deadlocked while closing an evicted entry"
    thread.join(timeout=5.0)

    assert victim.closed  # eviction side effect ran
    assert store.get("victim") is None
    assert store.get("survivor") is survivor
