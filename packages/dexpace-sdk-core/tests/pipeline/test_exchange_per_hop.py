# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""``ExchangeContext`` must record the per-hop request, not the original one.

The transport runner promotes the call's telemetry context to an
``ExchangeContext`` once the response is in hand. After a redirect the request
that actually produced the response (``Response.request``) differs from the
request the call started with. These tests pin that the recorded
``ExchangeContext.request`` is the response's own per-hop request, so observers
see a request/response pair that truly traveled together.

The ``ContextStore`` entry is evicted in ``Pipeline.run``'s ``finally`` once the
chain has fully unwound, so the snapshot is read from inside an in-chain
observer policy (on the response side) rather than after ``run`` returns.
"""

from __future__ import annotations

import asyncio

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import (
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
from dexpace.sdk.core.pipeline.policies.async_redirect import AsyncRedirectPolicy
from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request(url: str = "https://example.com/start") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


class _RedirectingClient(HttpClient):
    """First call 301s to a new location; second call returns 200 OK."""

    def __init__(self, target: str) -> None:
        self._target = target
        self.requests: list[Request] = []

    def execute(self, request: Request) -> Response:
        self.requests.append(request)
        if len(self.requests) == 1:
            moved = Response(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=Status.MOVED_PERMANENTLY,
            )
            return moved.with_header("Location", self._target)
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _AsyncRedirectingClient(AsyncHttpClient):
    """Async twin of ``_RedirectingClient``."""

    def __init__(self, target: str) -> None:
        self._target = target
        self.requests: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            moved = AsyncResponse(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=Status.MOVED_PERMANENTLY,
            )
            return moved.with_header("Location", self._target)
        return AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


def test_exchange_records_per_hop_request_after_redirect() -> None:
    """The recorded ``ExchangeContext.request`` is the redirected request."""
    instr = _instr("0" * 15 + "a1")
    target = "https://example.com/new"
    client = _RedirectingClient(target)
    seen: list[ExchangeContext | None] = []

    class _Observer(Policy):
        def send(self, request: Request, ctx: PipelineContext) -> Response:
            response = self.next.send(request, ctx)
            stored = ContextStore.get(instr.trace_id.value)
            assert isinstance(stored, ExchangeContext)
            seen.append(stored)
            return response

    with Pipeline(client, policies=[_Observer(), RedirectPolicy()]) as pipe:
        response = pipe.run(_request(), DispatchContext(instr))

    # Two hops were made; the final response carries the redirected request.
    assert len(client.requests) == 2
    assert str(response.request.url) == target
    assert str(response.request.url) != "https://example.com/start"

    exchange = seen[0]
    assert isinstance(exchange, ExchangeContext)
    # The exchange records the per-hop request that produced the response,
    # not the original request the call started with.
    assert exchange.request is response.request
    assert str(exchange.request.url) == target


def test_exchange_request_and_response_traveled_together() -> None:
    """``ExchangeContext.request`` is identity-equal to ``response.request``."""
    instr = _instr("0" * 15 + "a2")
    client = _RedirectingClient("https://example.com/elsewhere")
    seen: list[tuple[Request, Response | AsyncResponse]] = []

    class _Observer(Policy):
        def send(self, request: Request, ctx: PipelineContext) -> Response:
            response = self.next.send(request, ctx)
            stored = ContextStore.get(instr.trace_id.value)
            assert isinstance(stored, ExchangeContext)
            seen.append((stored.request, stored.response))
            return response

    with Pipeline(client, policies=[_Observer(), RedirectPolicy()]) as pipe:
        response = pipe.run(_request(), DispatchContext(instr))

    recorded_request, recorded_response = seen[0]
    assert recorded_request is response.request
    assert recorded_response is response


def test_async_exchange_records_per_hop_request_after_redirect() -> None:
    """Async transport runner records the redirected per-hop request too."""
    instr = _instr("0" * 15 + "a3")
    target = "https://example.com/async-new"
    client = _AsyncRedirectingClient(target)
    seen: list[ExchangeContext | None] = []

    class _Observer(AsyncPolicy):
        async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
            response = await self.next.send(request, ctx)
            stored = ContextStore.get(instr.trace_id.value)
            assert isinstance(stored, ExchangeContext)
            seen.append(stored)
            return response

    async def run() -> AsyncResponse:
        async with AsyncPipeline(client, policies=[_Observer(), AsyncRedirectPolicy()]) as pipe:
            return await pipe.run(_request(), DispatchContext(instr))

    response = asyncio.run(run())

    assert len(client.requests) == 2
    assert str(response.request.url) == target
    exchange = seen[0]
    assert isinstance(exchange, ExchangeContext)
    assert exchange.request is response.request
    assert str(exchange.request.url) == target
