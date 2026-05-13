"""Tests for ``SetDatePolicy``."""

from __future__ import annotations

import re
from email.utils import formatdate

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
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
from dexpace.sdk.core.pipeline.policies.set_date import SetDatePolicy

from ..conftest import FakeClock

_RFC7231 = re.compile(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$")


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


class _RecordingClient(HttpClient):
    """Captures the request handed to the transport for assertion."""

    def __init__(self) -> None:
        self.calls: list[Request] = []

    def execute(self, request: Request) -> Response:
        self.calls.append(request)
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


def test_date_header_stamped() -> None:
    client = _RecordingClient()
    with Pipeline(client, policies=[SetDatePolicy()]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
    stamped = client.calls[0].headers.get("Date")
    assert stamped is not None
    assert _RFC7231.match(stamped) is not None


def test_overwrites_existing_date() -> None:
    client = _RecordingClient()
    with Pipeline(client, policies=[SetDatePolicy()]) as p:
        p.run(_request(date="old-value"), DispatchContext(_instr("0" * 16 + "2")))
    stamped = client.calls[0].headers.get("Date")
    assert stamped is not None
    assert stamped != "old-value"
    assert _RFC7231.match(stamped) is not None


def test_uses_injected_clock() -> None:
    # 1700000000 -> "Tue, 14 Nov 2023 22:13:20 GMT"
    fixed = 1_700_000_000.0
    expected = formatdate(fixed, usegmt=True)
    clock = FakeClock(start=fixed)
    client = _RecordingClient()
    with Pipeline(client, policies=[SetDatePolicy(clock=clock)]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "3")))
    assert client.calls[0].headers.get("Date") == expected


def test_restamps_on_each_attempt() -> None:
    """A second send through the policy reads the clock again."""
    clock = FakeClock(start=1_700_000_000.0)
    client = _RecordingClient()
    with Pipeline(client, policies=[SetDatePolicy(clock=clock)]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
        clock.advance(3600)  # one hour later
        p.run(_request(), DispatchContext(_instr("0" * 16 + "5")))
    first = client.calls[0].headers.get("Date")
    second = client.calls[1].headers.get("Date")
    assert first is not None and second is not None
    assert first != second
    assert _RFC7231.match(first) is not None
    assert _RFC7231.match(second) is not None
