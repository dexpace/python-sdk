"""Tests for the ``DispatchContext`` → ``RequestContext`` → ``ExchangeContext`` chain."""
from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import Protocol
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
    return Request(method=Method.GET, url="https://example.com/")


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
