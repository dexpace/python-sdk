# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the ``DispatchContext`` → ``RequestContext`` → ``ExchangeContext`` chain."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import (
    ContextStore,
    DispatchContext,
    ExchangeContext,
)
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


def test_promotion_registers_in_context_store() -> None:
    instr = _instrumentation("0" * 16 + "1")
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(_request())
    key = request_ctx.store_key
    assert key is not None
    try:
        assert ContextStore.get(key) is request_ctx

        exchange = request_ctx.to_exchange_context(_response(request_ctx.request))
        # The exchange tier reuses the request tier's key, overwriting in place.
        assert exchange.store_key == key
        assert ContextStore.get(key) is exchange
    finally:
        request_ctx.close()


def test_close_removes_from_store() -> None:
    instr = _instrumentation("0" * 16 + "2")
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(_request())
    key = request_ctx.store_key
    assert key is not None
    assert ContextStore.get(key) is request_ctx
    request_ctx.close()
    assert ContextStore.get(key) is None


def test_close_is_idempotent() -> None:
    instr = _instrumentation("0" * 16 + "3")
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(_request())
    request_ctx.close()
    request_ctx.close()


def test_context_store_rejects_duplicate_put() -> None:
    instr = _instrumentation("0" * 16 + "4")
    dispatch = DispatchContext(instrumentation_context=instr)
    try:
        ContextStore.put(instr.trace_id.value, dispatch)
        with pytest.raises(ValueError, match="already registered"):
            ContextStore.put(instr.trace_id.value, dispatch)
    finally:
        ContextStore.remove(instr.trace_id.value)


def test_dispatch_noop_factory() -> None:
    dispatch = DispatchContext.noop()
    # No-op trace id is invalid, but the context is constructible.
    assert isinstance(dispatch, DispatchContext)
    assert not dispatch.instrumentation_context.is_valid


def test_context_store_concurrent_get_with_writes() -> None:
    """``ContextStore.get`` must be safe under concurrent ``set``/``remove``.

    The lock guarantees this on free-threaded CPython (PEP 703) and
    non-CPython runtimes; under the GIL this test is mostly a correctness
    demonstration.
    """
    instr = _instrumentation("0" * 16 + "6")
    dispatch = DispatchContext(instrumentation_context=instr)
    trace_id = instr.trace_id.value
    ContextStore.put(trace_id, dispatch)

    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            other_id = f"{i:032x}"[:32]
            ContextStore.set(other_id, dispatch)
            ContextStore.remove(other_id)
            i += 1

    def reader() -> list[bool]:
        seen: list[bool] = []
        for _ in range(500):
            seen.append(ContextStore.get(trace_id) is dispatch)
        return seen

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            writers = [pool.submit(writer) for _ in range(2)]
            readers = [pool.submit(reader) for _ in range(4)]
            try:
                results = [f.result(timeout=5) for f in readers]
            finally:
                stop.set()
                for f in writers:
                    f.result(timeout=5)
        for seen in results:
            assert all(seen)
    finally:
        ContextStore.remove(trace_id)


def test_exchange_context_carries_request_and_response() -> None:
    instr = _instrumentation("0" * 16 + "5")
    dispatch = DispatchContext(instrumentation_context=instr)
    req = _request()
    request_ctx = dispatch.to_request_context(req)
    try:
        resp = _response(req)
        exchange = request_ctx.to_exchange_context(resp)
        assert isinstance(exchange, ExchangeContext)
        # ExchangeContext does not inherit from RequestContext — promotion
        # copies the request reference rather than extending the parent class.
        assert exchange.request is req
        assert exchange.response is resp
    finally:
        request_ctx.close()


def test_concurrent_same_trace_id_both_register_and_resolve() -> None:
    """Two calls sharing a trace id must not collide in the store.

    A single traced operation that fans out produces two calls carrying the
    same trace id (and span id). Under a trace-id-only key the second
    registration would overwrite the first, and closing either would evict the
    survivor. The counter-suffixed key gives each call its own slot so both
    register, both resolve, and closing one leaves the other live.
    """
    instr = _instrumentation("0" * 16 + "a")
    rc1 = DispatchContext(instrumentation_context=instr).to_request_context(_request())
    rc2 = DispatchContext(instrumentation_context=instr).to_request_context(_request())
    key1, key2 = rc1.store_key, rc2.store_key
    assert key1 is not None and key2 is not None
    try:
        # Distinct store keys despite the shared trace id.
        assert key1 != key2
        # Both live and independently resolvable.
        assert ContextStore.get(key1) is rc1
        assert ContextStore.get(key2) is rc2
        # Closing one leaves the other untouched.
        rc1.close()
        assert ContextStore.get(key1) is None
        assert ContextStore.get(key2) is rc2
    finally:
        rc1.close()
        rc2.close()


def test_prefix_scan_by_trace_id_returns_all_live_contexts() -> None:
    """The trace id stays the leading key segment so a prefix scan still works.

    ``contexts_for_trace`` must return every live context for a trace id even
    when several calls share it, and each key must carry the trace id as its
    leading, colon-delimited segment.
    """
    instr = _instrumentation("0" * 16 + "b")
    trace_id = instr.trace_id.value
    rc1 = DispatchContext(instrumentation_context=instr).to_request_context(_request())
    rc2 = DispatchContext(instrumentation_context=instr).to_request_context(_request())
    try:
        found = ContextStore.contexts_for_trace(trace_id)
        assert {id(ctx) for ctx in found} == {id(rc1), id(rc2)}
        assert rc1.store_key is not None and rc1.store_key.startswith(f"{trace_id}:")
        assert rc2.store_key is not None and rc2.store_key.startswith(f"{trace_id}:")
        # Closing one shrinks the scan result to just the survivor.
        rc1.close()
        assert [id(ctx) for ctx in ContextStore.contexts_for_trace(trace_id)] == [id(rc2)]
    finally:
        rc1.close()
        rc2.close()


def test_context_store_stress_no_lost_or_corrupted_entries() -> None:
    """Many threads registering/closing calls that share a trace id.

    Every call draws its own counter-suffixed key, so each thread's
    register/resolve/close cycle is isolated: no key collides, no live entry is
    lost, and the store holds no leaked entries for the trace id once every
    worker finishes.
    """
    instr = _instrumentation("0" * 16 + "c")
    trace_id = instr.trace_id.value
    workers = 8
    per_worker = 250

    def worker() -> list[str]:
        keys: list[str] = []
        for _ in range(per_worker):
            rc = DispatchContext(instrumentation_context=instr).to_request_context(_request())
            key = rc.store_key
            assert key is not None
            keys.append(key)
            # Unique key ⇒ this lookup is deterministic even under contention.
            assert ContextStore.get(key) is rc
            rc.close()
            assert ContextStore.get(key) is None
        return keys

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker) for _ in range(workers)]
        all_keys = [key for future in futures for key in future.result(timeout=30)]

    # No two calls shared a key ⇒ every counter increment was atomic.
    assert len(all_keys) == len(set(all_keys)) == workers * per_worker
    # Every entry was evicted ⇒ no leak under the shared trace id.
    assert ContextStore.contexts_for_trace(trace_id) == []
