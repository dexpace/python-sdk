# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests that pipeline policies emit the expected ``HttpTracer`` events.

Covers the P7 emission seam: ``TracingPolicy`` drives the operation/request/
response lifecycle callbacks, and ``RedirectPolicy`` /
``AsyncRedirectPolicy`` emit ``request_url_resolved`` per hop. The custom
tracer below records every callback so each test can assert the exact
sequence the policies produced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.instrumentation import (
    HttpTracer,
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline
from dexpace.sdk.core.pipeline.policies import TracingPolicy
from dexpace.sdk.core.pipeline.policies.async_redirect import AsyncRedirectPolicy
from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy


class _RecordingHttpTracer(HttpTracer):
    """Captures every callback as a ``(name, payload)`` event tuple."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def operation_started(self) -> None:
        self.events.append(("operation_started", None))

    def operation_succeeded(self) -> None:
        self.events.append(("operation_succeeded", None))

    def operation_failed(self, error: BaseException) -> None:
        self.events.append(("operation_failed", error))

    def request_url_resolved(self, url: str) -> None:
        self.events.append(("request_url_resolved", url))

    def request_sent(self, byte_count: int) -> None:
        self.events.append(("request_sent", byte_count))

    def response_headers_received(self, status: int, headers: Mapping[str, str]) -> None:
        self.events.append(("response_headers_received", (status, dict(headers))))

    def response_received(self, byte_count: int) -> None:
        self.events.append(("response_received", byte_count))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class _Factory:
    """``HttpTracerFactory`` returning a single shared recording tracer."""

    def __init__(self, tracer: HttpTracer) -> None:
        self._tracer = tracer

    def create(self) -> HttpTracer:
        return self._tracer


def _instr(tracer: HttpTracer, trace: str = "0" * 31 + "1") -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 15 + "1"),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
        http_tracer_factory=_Factory(tracer),
    )


def _request(url: str = "https://api.example.com/v1") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


class _OkClient(HttpClient):
    def __init__(
        self,
        *,
        status: Status = Status.OK,
        body: ResponseBody | None = None,
        response_headers: tuple[tuple[str, str], ...] = (),
        raise_exc: BaseException | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.response_headers = response_headers
        self.raise_exc = raise_exc

    def execute(self, request: Request) -> Response:
        if self.raise_exc is not None:
            raise self.raise_exc
        response = Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=self.status,
            body=self.body,
        )
        for name, value in self.response_headers:
            response = response.with_header(name, value)
        return response


class TestTracingPolicyEmission:
    def test_emits_lifecycle_events_in_order_on_success(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        body = ResponseBody.from_bytes(b"hello world")
        with Pipeline(_OkClient(body=body), policies=[TracingPolicy()]) as p:
            p.run(_request(), DispatchContext(instr))
        assert tracer.names() == [
            "operation_started",
            "request_sent",
            "response_headers_received",
            "response_received",
            "operation_succeeded",
        ]

    def test_request_sent_reports_body_byte_count(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        req = Request(
            method=Method.POST,
            url=Url.parse("https://api.example.com/v1"),
            body=RequestBody.from_bytes(b"payload-12"),
        )
        with Pipeline(_OkClient(), policies=[TracingPolicy()]) as p:
            p.run(req, DispatchContext(instr))
        sent = [payload for name, payload in tracer.events if name == "request_sent"]
        assert sent == [len(b"payload-12")]

    def test_request_sent_zero_for_bodyless_request(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        with Pipeline(_OkClient(), policies=[TracingPolicy()]) as p:
            p.run(_request(), DispatchContext(instr))
        sent = [payload for name, payload in tracer.events if name == "request_sent"]
        assert sent == [0]

    def test_response_headers_event_carries_status_and_headers(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        client = _OkClient(status=Status.OK, response_headers=(("X-Trace", "abc"),))
        with Pipeline(client, policies=[TracingPolicy()]) as p:
            p.run(_request(), DispatchContext(instr))
        headers_events = [
            payload for name, payload in tracer.events if name == "response_headers_received"
        ]
        assert len(headers_events) == 1
        payload = headers_events[0]
        assert isinstance(payload, tuple)
        status, headers = payload
        assert status == 200
        assert isinstance(headers, Mapping)
        assert headers["x-trace"] == "abc"

    def test_emits_operation_failed_on_exception(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        boom = ServiceRequestError("connect failed")
        raised: BaseException | None = None
        with Pipeline(_OkClient(raise_exc=boom), policies=[TracingPolicy()]) as p:
            try:
                p.run(_request(), DispatchContext(instr))
            except ServiceRequestError as err:
                raised = err
        assert raised is boom
        assert tracer.names() == ["operation_started", "request_sent", "operation_failed"]
        failed = [payload for name, payload in tracer.events if name == "operation_failed"]
        assert failed == [boom]

    def test_no_events_when_tracing_disabled(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        with Pipeline(_OkClient(), policies=[TracingPolicy()]) as p:
            p.run(_request(), DispatchContext(instr), tracing_enabled=False)
        assert tracer.events == []


# ----- redirect hop emission (sync + async) ------------------------------


class _Hop:
    __slots__ = ("location", "status")

    def __init__(self, status: Status, location: str | None = None) -> None:
        self.status = status
        self.location = location


class _ScriptedClient(HttpClient):
    def __init__(self, hops: Sequence[_Hop]) -> None:
        self._hops = list(hops)
        self.requests: list[Request] = []

    def execute(self, request: Request) -> Response:
        idx = len(self.requests)
        self.requests.append(request)
        hop = self._hops[idx]
        response = Response(request=request, protocol=Protocol.HTTP_1_1, status=hop.status)
        if hop.location is not None:
            response = response.with_header("Location", hop.location)
        return response


class _ScriptedAsyncClient(AsyncHttpClient):
    def __init__(self, hops: Sequence[_Hop]) -> None:
        self._hops = list(hops)
        self.requests: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        idx = len(self.requests)
        self.requests.append(request)
        hop = self._hops[idx]
        response = AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=hop.status)
        if hop.location is not None:
            response = response.with_header("Location", hop.location)
        return response


class TestRedirectHopEmission:
    def test_emits_request_url_resolved_per_hop(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        client = _ScriptedClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://api.example.com/new"),
                _Hop(Status.OK),
            ],
        )
        with Pipeline(client, policies=[RedirectPolicy()]) as p:
            p.run(_request("https://api.example.com/start"), DispatchContext(instr))
        resolved = [payload for name, payload in tracer.events if name == "request_url_resolved"]
        assert resolved == [
            "https://api.example.com/start",
            "https://api.example.com/new",
        ]

    def test_single_hop_emits_only_initial_url(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        client = _ScriptedClient([_Hop(Status.OK)])
        with Pipeline(client, policies=[RedirectPolicy()]) as p:
            p.run(_request("https://api.example.com/start"), DispatchContext(instr))
        resolved = [payload for name, payload in tracer.events if name == "request_url_resolved"]
        assert resolved == ["https://api.example.com/start"]

    async def test_async_emits_request_url_resolved_per_hop(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        client = _ScriptedAsyncClient(
            [
                _Hop(Status.FOUND, "https://api.example.com/next"),
                _Hop(Status.OK),
            ],
        )
        async with AsyncPipeline(client, policies=[AsyncRedirectPolicy()]) as p:
            await p.run(
                _request("https://api.example.com/start"),
                DispatchContext(instr),
            )
        resolved = [payload for name, payload in tracer.events if name == "request_url_resolved"]
        assert resolved == [
            "https://api.example.com/start",
            "https://api.example.com/next",
        ]


class TestSharedTracerAcrossPolicies:
    def test_redirect_and_tracing_share_one_tracer(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        client = _ScriptedClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://api.example.com/new"),
                _Hop(Status.OK),
            ],
        )
        with Pipeline(
            client,
            policies=[RedirectPolicy(), TracingPolicy()],
        ) as p:
            p.run(_request("https://api.example.com/start"), DispatchContext(instr))
        names = tracer.names()
        # Both the redirect hop events and the operation lifecycle events land
        # on the same recording tracer instance.
        assert "request_url_resolved" in names
        assert "operation_started" in names
        assert "operation_succeeded" in names
        # Two hops -> two request_sent events from the inner TracingPolicy.
        assert names.count("request_url_resolved") == 2
