"""Tests for ``AsyncSetDatePolicy``."""

from __future__ import annotations

import re
from email.utils import formatdate

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
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
from dexpace.sdk.core.pipeline import AsyncPipeline
from dexpace.sdk.core.pipeline.policies.async_set_date import AsyncSetDatePolicy

_RFC7231 = re.compile(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$")


class _AsyncFakeClock:
    """Deterministic ``AsyncClock`` for tests."""

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    async def sleep(self, duration: float) -> None:
        self._t += max(0.0, duration)

    def advance(self, duration: float) -> None:
        self._t += duration


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request(*, date: str | None = None) -> Request:
    req = Request(method=Method.GET, url=Url.parse("https://api.example.com/v1"))
    if date is not None:
        req = req.with_header("Date", date)
    return req


class _RecordingAsyncClient(AsyncHttpClient):
    def __init__(self) -> None:
        self.calls: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        self.calls.append(request)
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.OK,
        )


async def test_date_header_stamped() -> None:
    client = _RecordingAsyncClient()
    async with AsyncPipeline(client, policies=[AsyncSetDatePolicy()]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
    stamped = client.calls[0].headers.get("Date")
    assert stamped is not None
    assert _RFC7231.match(stamped) is not None


async def test_overwrites_existing_date() -> None:
    client = _RecordingAsyncClient()
    async with AsyncPipeline(client, policies=[AsyncSetDatePolicy()]) as p:
        await p.run(
            _request(date="old-value"),
            DispatchContext(_instr("0" * 16 + "2")),
        )
    stamped = client.calls[0].headers.get("Date")
    assert stamped is not None
    assert stamped != "old-value"
    assert _RFC7231.match(stamped) is not None


async def test_uses_injected_clock() -> None:
    fixed = 1_700_000_000.0
    expected = formatdate(fixed, usegmt=True)
    clock = _AsyncFakeClock(start=fixed)
    client = _RecordingAsyncClient()
    async with AsyncPipeline(client, policies=[AsyncSetDatePolicy(clock=clock)]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "3")))
    assert client.calls[0].headers.get("Date") == expected


async def test_restamps_on_each_attempt() -> None:
    clock = _AsyncFakeClock(start=1_700_000_000.0)
    client = _RecordingAsyncClient()
    async with AsyncPipeline(client, policies=[AsyncSetDatePolicy(clock=clock)]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
        clock.advance(3600)
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "5")))
    first = client.calls[0].headers.get("Date")
    second = client.calls[1].headers.get("Date")
    assert first is not None and second is not None
    assert first != second
    assert _RFC7231.match(first) is not None
    assert _RFC7231.match(second) is not None
