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
from dexpace.sdk.core.pipeline.policies import (
    OperationTracingPolicy,
    RetryPolicy,
    TracingPolicy,
)
from dexpace.sdk.core.pipeline.policies.async_redirect import AsyncRedirectPolicy
from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy

from ..conftest import FakeClock


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

    def request_sent(self, byte_count: int | None) -> None:
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
        with Pipeline(
            _OkClient(body=body),
            policies=[OperationTracingPolicy(), TracingPolicy()],
        ) as p:
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
        with Pipeline(
            _OkClient(raise_exc=boom),
            policies=[OperationTracingPolicy(), TracingPolicy()],
        ) as p:
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
        with Pipeline(
            _OkClient(),
            policies=[OperationTracingPolicy(), TracingPolicy()],
        ) as p:
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
            policies=[OperationTracingPolicy(), RedirectPolicy(), TracingPolicy()],
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


# ----- operation_* fire once per operation, not per attempt/hop ----------


class _RetryThenOkClient(HttpClient):
    """Returns ``fail_status`` for the first ``fail_count`` calls, then OK."""

    def __init__(self, *, fail_status: Status, fail_count: int) -> None:
        self._fail_status = fail_status
        self._fail_count = fail_count
        self.calls = 0

    def execute(self, request: Request) -> Response:
        status = self._fail_status if self.calls < self._fail_count else Status.OK
        self.calls += 1
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=status)


class _AlwaysRaisesClient(HttpClient):
    """Raises a retryable error on every call to exhaust the retry budget."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def execute(self, request: Request) -> Response:
        self.calls += 1
        raise self._error


class _RaiseThenOkClient(HttpClient):
    """Raises a retryable error on the first ``fail_count`` calls, then OK."""

    def __init__(self, *, error: BaseException, fail_count: int) -> None:
        self._error = error
        self._fail_count = fail_count
        self.calls = 0

    def execute(self, request: Request) -> Response:
        self.calls += 1
        if self.calls <= self._fail_count:
            raise self._error
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _RedirectThenRaiseClient(HttpClient):
    """Returns a redirect, then raises on the reissued (post-redirect) request."""

    def __init__(self, *, location: str, error: BaseException) -> None:
        self._location = location
        self._error = error
        self.calls = 0

    def execute(self, request: Request) -> Response:
        self.calls += 1
        if self.calls == 1:
            response = Response(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=Status.MOVED_PERMANENTLY,
            )
            return response.with_header("Location", self._location)
        raise self._error


class TestOperationEventsFireOncePerOperation:
    def test_retry_emits_operation_lifecycle_once_across_attempts(self) -> None:
        # TracingPolicy sits *inside* RetryPolicy (retry is outer), so it is
        # re-entered once per attempt. The operation_* events must still fire
        # exactly once for the whole operation.
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        client = _RetryThenOkClient(fail_status=Status.SERVICE_UNAVAILABLE, fail_count=2)
        retry = RetryPolicy(status_retries=3, clock=FakeClock())
        with Pipeline(
            client,
            policies=[OperationTracingPolicy(), retry, TracingPolicy()],
        ) as p:
            p.run(_request(), DispatchContext(instr))
        names = tracer.names()
        # Three attempts total (two 503s then a 200).
        assert client.calls == 3
        assert names.count("operation_started") == 1
        assert names.count("operation_succeeded") == 1
        assert names.count("operation_failed") == 0
        # Per-attempt request_sent still fires once per attempt.
        assert names.count("request_sent") == 3
        # operation_started is the very first event the tracer sees, and it
        # precedes the single operation_succeeded.
        assert names[0] == "operation_started"
        assert names.index("operation_started") < names.index("operation_succeeded")

    def test_retry_exhaustion_emits_operation_failed_once(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        boom = ServiceRequestError("connect failed")
        client = _AlwaysRaisesClient(boom)
        retry = RetryPolicy(connect_retries=2, total_retries=2, clock=FakeClock())
        raised: BaseException | None = None
        with Pipeline(
            client,
            policies=[OperationTracingPolicy(), retry, TracingPolicy()],
        ) as p:
            try:
                p.run(_request(), DispatchContext(instr))
            except ServiceRequestError as err:
                raised = err
        assert raised is boom
        names = tracer.names()
        assert names.count("operation_started") == 1
        assert names.count("operation_failed") == 1
        assert names.count("operation_succeeded") == 0
        failed = [payload for name, payload in tracer.events if name == "operation_failed"]
        assert failed == [boom]

    def test_redirect_emits_operation_lifecycle_once_across_hops(self) -> None:
        # TracingPolicy sits *inside* RedirectPolicy (redirect is outer), so it
        # is re-entered per hop. The operation_* events fire exactly once.
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
            policies=[OperationTracingPolicy(), RedirectPolicy(), TracingPolicy()],
        ) as p:
            p.run(_request("https://api.example.com/start"), DispatchContext(instr))
        names = tracer.names()
        # Two hops -> two attempts through the inner TracingPolicy.
        assert names.count("request_sent") == 2
        assert names.count("operation_started") == 1
        assert names.count("operation_succeeded") == 1
        assert names.count("operation_failed") == 0
        # The redirect policy emits request_url_resolved before TracingPolicy
        # runs, so among the operation_* events, started precedes succeeded.
        op_events = [name for name in names if name.startswith("operation_")]
        assert op_events == ["operation_started", "operation_succeeded"]

    def test_retry_then_success_reports_operation_succeeded(self) -> None:
        # A call that fails on its first attempt and succeeds on a retry must
        # report a single operation_succeeded reflecting the final outcome —
        # never operation_failed for the discarded first attempt.
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        client = _RaiseThenOkClient(error=ServiceRequestError("connect failed"), fail_count=1)
        retry = RetryPolicy(connect_retries=3, total_retries=3, clock=FakeClock())
        with Pipeline(
            client,
            policies=[OperationTracingPolicy(), retry, TracingPolicy()],
        ) as p:
            response = p.run(_request(), DispatchContext(instr))
        assert int(response.status) == 200
        assert client.calls == 2
        names = tracer.names()
        assert names.count("operation_started") == 1
        assert names.count("operation_succeeded") == 1
        assert names.count("operation_failed") == 0
        # request_sent still fires per attempt (the failed one and the retry).
        assert names.count("request_sent") == 2

    def test_redirect_then_failure_reports_operation_failed(self) -> None:
        # When a later redirect hop fails, the operation outcome is the failure
        # that escapes — not the success of the earlier 3xx hop.
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        boom = ServiceRequestError("connect failed")
        client = _RedirectThenRaiseClient(location="https://api.example.com/new", error=boom)
        raised: BaseException | None = None
        with Pipeline(
            client,
            policies=[OperationTracingPolicy(), RedirectPolicy(), TracingPolicy()],
        ) as p:
            try:
                p.run(_request("https://api.example.com/start"), DispatchContext(instr))
            except ServiceRequestError as err:
                raised = err
        assert raised is boom
        names = tracer.names()
        assert names.count("operation_started") == 1
        assert names.count("operation_failed") == 1
        assert names.count("operation_succeeded") == 0
        failed = [payload for name, payload in tracer.events if name == "operation_failed"]
        assert failed == [boom]


# ----- request_sent fires for unknown-length bodies (L19) -----------------


class TestRequestSentUnknownLength:
    def test_request_sent_fires_with_none_for_unknown_length_body(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        body = RequestBody.from_iter([b"chunk-1", b"chunk-2"], content_length=-1)
        req = Request(
            method=Method.POST,
            url=Url.parse("https://api.example.com/v1"),
            body=body,
        )
        with Pipeline(_OkClient(), policies=[TracingPolicy()]) as p:
            p.run(req, DispatchContext(instr))
        sent = [payload for name, payload in tracer.events if name == "request_sent"]
        # The event still fires; the unknown length is reported as None, mirroring
        # the bodyless case which reports 0.
        assert sent == [None]

    def test_request_sent_reports_known_length_for_sized_stream(self) -> None:
        tracer = _RecordingHttpTracer()
        instr = _instr(tracer)
        body = RequestBody.from_iter([b"abc"], content_length=3)
        req = Request(
            method=Method.POST,
            url=Url.parse("https://api.example.com/v1"),
            body=body,
        )
        with Pipeline(_OkClient(), policies=[TracingPolicy()]) as p:
            p.run(req, DispatchContext(instr))
        sent = [payload for name, payload in tracer.events if name == "request_sent"]
        assert sent == [3]
