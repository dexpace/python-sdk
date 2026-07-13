# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the ``DispatchContext`` → ``RequestContext`` → ``ExchangeContext`` chain."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import (
    CallContext,
    ContextStore,
    DispatchContext,
    ExchangeContext,
    RequestContext,
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
    try:
        request_ctx = dispatch.to_request_context(_request())
        assert ContextStore.get(instr.trace_id.value) is request_ctx

        exchange = request_ctx.to_exchange_context(_response(request_ctx.request))
        assert ContextStore.get(instr.trace_id.value) is exchange
    finally:
        ContextStore.remove(instr.trace_id.value)


def test_close_removes_from_store() -> None:
    instr = _instrumentation("0" * 16 + "2")
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(_request())
    assert ContextStore.get(instr.trace_id.value) is request_ctx
    request_ctx.close()
    assert ContextStore.get(instr.trace_id.value) is None


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
        ContextStore.remove(instr.trace_id.value)


# --- Promotion-chain conformance certification (PY-M3-01) --------------------

_CONTEXT_CLASSES: tuple[type[CallContext], ...] = (
    CallContext,
    DispatchContext,
    RequestContext,
    ExchangeContext,
)


def test_all_links_are_call_contexts() -> None:
    """Every link in the chain is a `CallContext` subclass."""
    assert issubclass(DispatchContext, CallContext)
    assert issubclass(RequestContext, CallContext)
    assert issubclass(ExchangeContext, CallContext)


def test_all_links_are_context_managers() -> None:
    """Each concrete context implements the context-manager protocol."""
    for cls in (DispatchContext, RequestContext, ExchangeContext):
        assert hasattr(cls, "__enter__")
        assert hasattr(cls, "__exit__")


def test_every_link_carries_instrumentation_context() -> None:
    """Each concrete context exposes an `InstrumentationContext`."""
    instr = _instrumentation("0" * 16 + "7")
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(_request())
    try:
        exchange = request_ctx.to_exchange_context(_response(request_ctx.request))
        for ctx in (dispatch, request_ctx, exchange):
            assert isinstance(ctx.instrumentation_context, InstrumentationContext)
    finally:
        ContextStore.remove(instr.trace_id.value)


def test_promotion_shares_one_bundle_not_three_copies() -> None:
    """Promotion reuses the same payload object; it never copies it.

    The `InstrumentationContext` is the single shared bundle threaded through
    ``DispatchContext`` -> ``RequestContext`` -> ``ExchangeContext``, and the
    per-hop ``Request`` object is likewise shared by reference — proving each
    promotion promotes one bundle rather than materialising an independent copy.
    """
    instr = _instrumentation("0" * 16 + "8")
    req = _request()
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(req)
    try:
        exchange = request_ctx.to_exchange_context(_response(req))
        # The instrumentation bundle is the *same object* end to end.
        assert dispatch.instrumentation_context is instr
        assert request_ctx.instrumentation_context is instr
        assert exchange.instrumentation_context is instr
        # The request reference is shared, not deep-copied, across promotion.
        assert request_ctx.request is req
        assert exchange.request is req
    finally:
        ContextStore.remove(instr.trace_id.value)


def test_registration_happens_at_promotion_not_construction() -> None:
    """Constructing a `DispatchContext` does not touch the store.

    Registration is a side effect of promotion, so a freshly built dispatch
    context is absent from the store until ``to_request_context`` runs.
    """
    instr = _instrumentation("0" * 16 + "9")
    trace_id = instr.trace_id.value
    dispatch = DispatchContext(instrumentation_context=instr)
    assert ContextStore.get(trace_id) is None
    try:
        request_ctx = dispatch.to_request_context(_request())
        assert ContextStore.get(trace_id) is request_ctx
    finally:
        ContextStore.remove(trace_id)


def test_with_block_evicts_context_on_exit() -> None:
    """A ``with`` block registers, exposes, then evicts the context on exit."""
    instr = _instrumentation("0" * 16 + "a")
    trace_id = instr.trace_id.value
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(_request())
    assert ContextStore.get(trace_id) is request_ctx
    with request_ctx as entered:
        assert entered is request_ctx
        assert ContextStore.get(trace_id) is request_ctx
    assert ContextStore.get(trace_id) is None


def test_with_block_evicts_even_when_body_raises() -> None:
    """The context is evicted on exit even if the ``with`` body raises."""
    instr = _instrumentation("0" * 16 + "b")
    trace_id = instr.trace_id.value
    dispatch = DispatchContext(instrumentation_context=instr)
    request_ctx = dispatch.to_request_context(_request())
    with pytest.raises(RuntimeError, match="boom"), request_ctx:
        assert ContextStore.get(trace_id) is request_ctx
        raise RuntimeError("boom")
    assert ContextStore.get(trace_id) is None


def test_no_context_class_relies_on_del() -> None:
    """No context class defines ``__del__``.

    GC finalisers must never be the eviction mechanism; eviction is explicit
    via ``close`` / the ``with`` block. This audits the class namespaces (not
    just instances) so an inherited or dataclass-synthesised finaliser would
    still be caught.
    """
    for cls in _CONTEXT_CLASSES:
        assert "__del__" not in cls.__dict__, f"{cls.__name__} must not define __del__"
        assert getattr(cls, "__del__", None) is None
