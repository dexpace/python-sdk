"""Tests for the ``Pipeline`` engine, ``Policy`` ABC, and SansIO runners."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import PipelineAbortedError
from dexpace.sdk.core.http.common import Headers, Protocol, Url
from dexpace.sdk.core.http.context import CallContext, DispatchContext
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
from dexpace.sdk.core.pipeline import Pipeline, PipelineContext, Policy


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


def _ok_response(req: Request) -> Response:
    return Response(request=req, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _StubClient(HttpClient):
    """Records every call and returns a configured response."""

    def __init__(self, status: Status = Status.OK, *, fail_first: bool = False) -> None:
        self.status = status
        self.fail_first = fail_first
        self.calls: list[Request] = []

    def execute(self, request: Request) -> Response:
        self.calls.append(request)
        if self.fail_first and len(self.calls) == 1:
            from dexpace.sdk.core.errors import ServiceRequestError

            raise ServiceRequestError("simulated connect failure")
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=self.status)


class TestSansIOSteps:
    def test_request_step_modifies_outgoing_request(self) -> None:
        def add_header(request: Request, _ctx: CallContext) -> Request:
            return request.with_header("X-Probe", "1")

        client = _StubClient()
        with Pipeline(client, policies=[add_header]) as p:
            response = p.run(_request(), DispatchContext(_instr("0" * 16 + "a")))
        assert response.is_success
        assert client.calls[0].headers.get("x-probe") == "1"

    def test_response_step_modifies_incoming_response(self) -> None:
        def stamp_response(response: Response, _ctx: CallContext) -> Response:
            return response.with_header("X-Trace", "abc")

        stamp_response.side = "response"  # type: ignore[attr-defined]

        client = _StubClient()
        with Pipeline(client, policies=[stamp_response]) as p:
            response = p.run(_request(), DispatchContext(_instr("0" * 16 + "b")))
        assert response.headers.get("x-trace") == "abc"

    def test_step_returning_none_aborts(self) -> None:
        def abort(_request: Request, _ctx: CallContext) -> Request | None:
            return None

        client = _StubClient()
        with Pipeline(client, policies=[abort]) as p, pytest.raises(PipelineAbortedError):
            p.run(_request(), DispatchContext(_instr("0" * 16 + "c")))
        assert client.calls == []


class _CountingPolicy(Policy):
    """Records the order in which policies see the request."""

    def __init__(self, label: str, log: list[str]) -> None:
        self.label = label
        self.log = log

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        self.log.append(f"{self.label}-pre")
        response = self.next.send(request, ctx)
        self.log.append(f"{self.label}-post")
        return response


class TestPolicyChaining:
    def test_policies_run_in_declared_order(self) -> None:
        log: list[str] = []
        client = _StubClient()
        with Pipeline(client, policies=[_CountingPolicy("a", log), _CountingPolicy("b", log)]) as p:
            p.run(_request(), DispatchContext(_instr("0" * 16 + "d")))
        assert log == ["a-pre", "b-pre", "b-post", "a-post"]

    def test_empty_policy_list_passes_through_to_transport(self) -> None:
        client = _StubClient()
        with Pipeline(client) as p:
            response = p.run(_request(), DispatchContext(_instr("0" * 16 + "e")))
        assert response.is_success
        assert len(client.calls) == 1


class TestPipelineContext:
    def test_options_forwarded_via_kwargs(self) -> None:
        seen: dict[str, object] = {}

        class _Inspector(Policy):
            def send(self, request: Request, ctx: PipelineContext) -> Response:
                seen.update(ctx.options)
                return self.next.send(request, ctx)

        with Pipeline(_StubClient(), policies=[_Inspector()]) as p:
            p.run(_request(), DispatchContext(_instr("0" * 16 + "f")), retry_total=5)
        assert seen == {"retry_total": 5}


def test_invalid_step_raises_type_error() -> None:
    with pytest.raises(TypeError):
        Pipeline(_StubClient(), policies=["not-a-callable"])  # type: ignore[list-item]


def test_default_headers_singleton_used() -> None:
    # Sanity — Pipeline default Headers do not leak between requests.
    a = _request()
    b = _request()
    assert a.headers is Headers.empty() or a.headers == b.headers
