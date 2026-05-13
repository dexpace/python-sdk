"""Regression tests for issues discovered in the line-by-line review."""

from __future__ import annotations

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Headers, Protocol, Url
from dexpace.sdk.core.http.context import ContextStore, DispatchContext
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
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies import RetryPolicy

from ..conftest import FakeClock


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _get() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


class _ScriptedClient(HttpClient):
    def __init__(self, status: Status, *, headers: Headers | None = None) -> None:
        self.status = status
        self.headers = headers or Headers()
        self.calls = 0

    def execute(self, request: Request) -> Response:
        self.calls += 1
        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=self.status,
            headers=self.headers,
        )


def test_retry_after_does_not_force_retry_on_non_allowlisted_status() -> None:
    """Fix #7: a 400 with ``Retry-After`` must NOT be retried."""
    client = _ScriptedClient(
        Status.BAD_REQUEST,
        headers=Headers([("Retry-After", "5")]),
    )
    retry = RetryPolicy(clock=FakeClock())
    with Pipeline(client, policies=[retry]) as pipe:
        response = pipe.run(_get(), DispatchContext(_instr("0" * 16 + "1")))
    assert client.calls == 1
    assert response.status is Status.BAD_REQUEST


def test_retry_options_preserved_in_ctx_options() -> None:
    """Fix #8: ``ctx.options`` keys read by retry must remain visible to later policies."""
    captured: dict[str, object] = {}

    from dexpace.sdk.core.pipeline import PipelineContext, Policy

    class _Inspector(Policy):
        def send(self, request: Request, ctx: PipelineContext) -> Response:
            response = self.next.send(request, ctx)
            captured.update(ctx.options)
            return response

    client = _ScriptedClient(Status.OK)
    retry = RetryPolicy(clock=FakeClock())
    with Pipeline(client, policies=[_Inspector(), retry]) as pipe:
        pipe.run(
            _get(),
            DispatchContext(_instr("0" * 16 + "2")),
            retry_total=5,
            timeout=10.0,
        )
    assert captured == {"retry_total": 5, "timeout": 10.0}


def test_async_transport_runner_promotes_context() -> None:
    """Fix #6: the async pipeline records the ExchangeContext after the call."""
    import asyncio

    from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
    from dexpace.sdk.core.http.response import AsyncResponse
    from dexpace.sdk.core.pipeline import AsyncPipeline

    class _Client(AsyncHttpClient):
        async def execute(self, request: Request) -> AsyncResponse:
            return AsyncResponse(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=Status.OK,
            )

    instr = _instr("0" * 16 + "3")

    async def run() -> None:
        async with AsyncPipeline(_Client()) as pipe:
            await pipe.run(_get(), DispatchContext(instr))

    asyncio.run(run())
    stored = ContextStore.get(instr.trace_id.value)
    # The exchange context is the latest snapshot in the store.
    from dexpace.sdk.core.http.context import ExchangeContext

    assert isinstance(stored, ExchangeContext)
