"""Tests for ``AsyncPipeline``, ``AsyncPolicy``, and async SansIO runners."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.errors import PipelineAbortedError, ServiceRequestError
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import CallContext, DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import AsyncPipeline, AsyncPolicy, PipelineContext
from dexpace.sdk.core.pipeline.policies import AsyncRetryPolicy


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


class _StubAsyncClient(AsyncHttpClient):
    """Returns a configured async response, optionally failing the first call."""

    def __init__(self, status: Status = Status.OK, *, fail_first: bool = False) -> None:
        self.status = status
        self.fail_first = fail_first
        self.calls: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        self.calls.append(request)
        if self.fail_first and len(self.calls) == 1:
            raise ServiceRequestError("simulated connect failure")
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=self.status,
        )


async def _no_sleep(_duration: float) -> None:
    return None


async def test_request_step_modifies_request() -> None:
    def add_header(request: Request, _ctx: CallContext) -> Request:
        return request.with_header("X-Probe", "1")

    client = _StubAsyncClient()
    async with AsyncPipeline(client, policies=[add_header]) as p:
        response = await p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
    assert response.is_success
    assert client.calls[0].headers.get("x-probe") == "1"


async def test_async_step_supported() -> None:
    async def add_header(request: Request, _ctx: CallContext) -> Request:
        return request.with_header("X-Async", "yes")

    client = _StubAsyncClient()
    async with AsyncPipeline(client, policies=[add_header]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "2")))
    assert client.calls[0].headers.get("x-async") == "yes"


async def test_response_step_modifies_response() -> None:
    def stamp(response: AsyncResponse, _ctx: CallContext) -> AsyncResponse:
        return response.with_header("X-Trace", "abc")

    stamp.side = "response"  # type: ignore[attr-defined]

    async with AsyncPipeline(_StubAsyncClient(), policies=[stamp]) as p:
        response = await p.run(_request(), DispatchContext(_instr("0" * 16 + "3")))
    assert response.headers.get("x-trace") == "abc"


async def test_step_returning_none_aborts() -> None:
    def abort(_request: Request, _ctx: CallContext) -> Request | None:
        return None

    client = _StubAsyncClient()
    async with AsyncPipeline(client, policies=[abort]) as p:
        with pytest.raises(PipelineAbortedError):
            await p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
    assert client.calls == []


class _CountingPolicy(AsyncPolicy):
    def __init__(self, label: str, log: list[str]) -> None:
        self.label = label
        self.log = log

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        self.log.append(f"{self.label}-pre")
        response = await self.next.send(request, ctx)
        self.log.append(f"{self.label}-post")
        return response


async def test_policy_chain_order() -> None:
    log: list[str] = []
    async with AsyncPipeline(
        _StubAsyncClient(),
        policies=[_CountingPolicy("a", log), _CountingPolicy("b", log)],
    ) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "5")))
    assert log == ["a-pre", "b-pre", "b-post", "a-post"]


async def test_async_retry_recovers_from_connect_error() -> None:
    client = _StubAsyncClient(fail_first=True)
    retry = AsyncRetryPolicy(sleep=_no_sleep)
    async with AsyncPipeline(client, policies=[retry]) as p:
        response = await p.run(_request(), DispatchContext(_instr("0" * 16 + "6")))
    assert response.is_success
    assert len(client.calls) == 2


async def test_async_retry_on_503() -> None:
    class _ScriptedClient(AsyncHttpClient):
        def __init__(self) -> None:
            self.statuses = iter([Status.SERVICE_UNAVAILABLE, Status.OK])
            self.attempts = 0

        async def execute(self, request: Request) -> AsyncResponse:
            self.attempts += 1
            return AsyncResponse(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=next(self.statuses),
            )

    client = _ScriptedClient()
    retry = AsyncRetryPolicy(sleep=_no_sleep)
    async with AsyncPipeline(client, policies=[retry]) as p:
        response = await p.run(_request(), DispatchContext(_instr("0" * 16 + "7")))
    assert response.is_success
    assert client.attempts == 2


async def test_async_retry_with_single_use_body_auto_replays() -> None:
    consumed: list[bytes] = []

    class _BodyRecordingAsyncClient(AsyncHttpClient):
        def __init__(self) -> None:
            self._statuses = iter(
                [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK]
            )
            self.attempts = 0

        async def execute(self, request: Request) -> AsyncResponse:
            body = request.body
            captured = b"".join(body.iter_bytes()) if body is not None else b""
            consumed.append(captured)
            self.attempts += 1
            return AsyncResponse(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=next(self._statuses),
            )

    body = RequestBody.from_iter(iter([b"hello", b"world"]))
    request = Request(method=Method.POST, url=Url.parse("https://example.com/"), body=body)
    client = _BodyRecordingAsyncClient()
    retry = AsyncRetryPolicy(total_retries=2, backoff_factor=0, sleep=_no_sleep)
    async with AsyncPipeline(client, policies=[retry]) as p:
        response = await p.run(request, DispatchContext(_instr("0" * 16 + "8")))
    assert response.is_success
    assert consumed == [b"helloworld", b"helloworld", b"helloworld"]


async def test_async_retry_count_not_set_when_no_retry_happens() -> None:
    captured: dict[str, object] = {}

    class _Probe(AsyncPolicy):
        async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
            response = await self.next.send(request, ctx)
            captured["retry_count"] = ctx.data.get("retry_count")
            return response

    client = _StubAsyncClient()
    retry = AsyncRetryPolicy(sleep=_no_sleep)
    async with AsyncPipeline(client, policies=[_Probe(), retry]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "9")))
    # First-attempt success — no retry decision is committed, so
    # ``retry_count`` is never written.
    assert captured["retry_count"] is None


async def test_async_retry_count_set_when_retry_happens() -> None:
    captured: dict[str, object] = {}

    class _Probe(AsyncPolicy):
        async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
            response = await self.next.send(request, ctx)
            captured["retry_count"] = ctx.data.get("retry_count")
            return response

    client = _StubAsyncClient(fail_first=True)
    retry = AsyncRetryPolicy(sleep=_no_sleep)
    async with AsyncPipeline(client, policies=[_Probe(), retry]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 15 + "10")))
    assert captured["retry_count"] == 1


def test_invalid_step_raises_type_error() -> None:
    with pytest.raises(TypeError):
        AsyncPipeline(_StubAsyncClient(), policies=["not callable"])  # type: ignore[list-item]
