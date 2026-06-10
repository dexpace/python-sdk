# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pipeline must evict its ``ContextStore`` entry once a call completes.

The promotion chain registers the call's context in the process-wide
``ContextStore`` keyed by trace id. Without eviction the store grows without
bound across calls with distinct trace ids. ``Pipeline.run`` /
``AsyncPipeline.run`` evict the entry after the chain has fully unwound — i.e.
after every in-chain observer has read the latest snapshot.
"""

from __future__ import annotations

import asyncio

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import (
    CallContext,
    ContextStore,
    DispatchContext,
    ExchangeContext,
)
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import (
    AsyncPipeline,
    AsyncPolicy,
    Pipeline,
    PipelineContext,
    Policy,
)


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


class _StubClient(HttpClient):
    def execute(self, request: Request) -> Response:
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _FailingClient(HttpClient):
    def execute(self, request: Request) -> Response:
        raise ServiceRequestError("boom")


class _StubAsyncClient(AsyncHttpClient):
    async def execute(self, request: Request) -> AsyncResponse:
        return AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _FailingAsyncClient(AsyncHttpClient):
    async def execute(self, request: Request) -> AsyncResponse:
        raise ServiceRequestError("boom")


def test_run_evicts_context_store_entry() -> None:
    instr = _instr("0" * 16 + "1")
    with Pipeline(_StubClient()) as pipe:
        pipe.run(_request(), DispatchContext(instr))
    assert ContextStore.get(instr.trace_id.value) is None
    assert ContextStore._contexts == {}


def test_run_evicts_even_when_chain_raises() -> None:
    instr = _instr("0" * 16 + "2")
    with Pipeline(_FailingClient()) as pipe, pytest.raises(ServiceRequestError):
        pipe.run(_request(), DispatchContext(instr))
    assert ContextStore.get(instr.trace_id.value) is None


def test_in_chain_observer_still_sees_exchange_context() -> None:
    """A response-side observer reading the store mid-unwind sees the exchange."""
    instr = _instr("0" * 16 + "3")
    seen: list[CallContext | None] = []

    class _Observer(Policy):
        def send(self, request: Request, ctx: PipelineContext) -> Response:
            response = self.next.send(request, ctx)
            seen.append(ContextStore.get(instr.trace_id.value))
            return response

    with Pipeline(_StubClient(), policies=[_Observer()]) as pipe:
        pipe.run(_request(), DispatchContext(instr))

    assert len(seen) == 1
    assert isinstance(seen[0], ExchangeContext)
    # Once run() returns, the entry is gone.
    assert ContextStore.get(instr.trace_id.value) is None


def test_async_run_evicts_context_store_entry() -> None:
    instr = _instr("0" * 16 + "4")

    async def run() -> None:
        async with AsyncPipeline(_StubAsyncClient()) as pipe:
            await pipe.run(_request(), DispatchContext(instr))

    asyncio.run(run())
    assert ContextStore.get(instr.trace_id.value) is None
    assert ContextStore._contexts == {}


def test_async_run_evicts_even_when_chain_raises() -> None:
    instr = _instr("0" * 16 + "6")

    async def run() -> None:
        async with AsyncPipeline(_FailingAsyncClient()) as pipe:
            await pipe.run(_request(), DispatchContext(instr))

    with pytest.raises(ServiceRequestError):
        asyncio.run(run())
    assert ContextStore.get(instr.trace_id.value) is None


def test_async_in_chain_observer_still_sees_exchange_context() -> None:
    instr = _instr("0" * 16 + "5")
    seen: list[CallContext | None] = []

    class _Observer(AsyncPolicy):
        async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
            response = await self.next.send(request, ctx)
            seen.append(ContextStore.get(instr.trace_id.value))
            return response

    async def run() -> None:
        async with AsyncPipeline(_StubAsyncClient(), policies=[_Observer()]) as pipe:
            await pipe.run(_request(), DispatchContext(instr))

    asyncio.run(run())
    assert len(seen) == 1
    assert isinstance(seen[0], ExchangeContext)
    assert ContextStore.get(instr.trace_id.value) is None
