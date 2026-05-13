"""Tests for ``LoggingPolicy`` and ``TracingPolicy``."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from _pytest.logging import LogCaptureFixture

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import ServiceRequestError
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
    Tracer,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies import LoggingPolicy, RetryPolicy, TracingPolicy


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request(url: str = "https://api.example.com/v1?token=secret&api-version=1.0") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


class _OkClient(HttpClient):
    def __init__(
        self, *, status: Status = Status.OK, raise_exc: BaseException | None = None
    ) -> None:
        self.status = status
        self.raise_exc = raise_exc

    def execute(self, request: Request) -> Response:
        if self.raise_exc is not None:
            raise self.raise_exc
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=self.status)


class TestLoggingPolicy:
    def test_emits_request_and_response(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(_OkClient(), policies=[LoggingPolicy()]) as p:
            p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("http.request" in m for m in messages)
        assert any("http.response" in m for m in messages)
        # URL is redacted — token value must not appear in the log.
        joined = " ".join(messages)
        assert "secret" not in joined
        assert "REDACTED" in joined

    def test_logs_error_on_exception(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.ERROR, logger="dexpace.sdk.core.http")
        boom = ServiceRequestError("connect failed")
        with (
            Pipeline(_OkClient(raise_exc=boom), policies=[LoggingPolicy()]) as p,
            pytest.raises(ServiceRequestError),
        ):
            p.run(_request(), DispatchContext(_instr("0" * 16 + "2")))
        assert any(
            "http.error" in rec.getMessage() and "ServiceRequestError" in rec.getMessage()
            for rec in caplog.records
        )

    def test_opt_out_via_options(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(_OkClient(), policies=[LoggingPolicy()]) as p:
            p.run(
                _request(),
                DispatchContext(_instr("0" * 16 + "3")),
                logging_enabled=False,
            )
        assert not caplog.records


class _RecordingSpan:
    """In-memory ``Span`` that captures every method call for assertions."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.error_type: str | None = None
        self.ended = False
        self.end_error: BaseException | None = None
        self.scope_active = False

    @property
    def is_recording(self) -> bool:
        return True

    @property
    def context(self) -> InstrumentationContext:
        return _instr("0" * 16 + "f")

    def set_attribute(self, key: str, value: Any) -> _RecordingSpan:
        self.attributes[key] = value
        return self

    def set_error(self, error_type: str) -> _RecordingSpan:
        self.error_type = error_type
        return self

    def make_current(self) -> _RecordingScope:
        return _RecordingScope(self)

    def end(self, error: BaseException | None = None) -> None:
        self.ended = True
        self.end_error = error


class _RecordingScope:
    def __init__(self, span: _RecordingSpan) -> None:
        self.span = span

    def close(self) -> None:
        self.span.scope_active = False

    def __enter__(self) -> _RecordingScope:
        self.span.scope_active = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _RecordingTracer(Tracer):
    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    def start_span(
        self,
        name: str,
        parent: InstrumentationContext | None = None,
    ) -> Any:  # _RecordingSpan satisfies the Span protocol structurally
        del name, parent
        span = _RecordingSpan()
        self.spans.append(span)
        return span


class TestTracingPolicy:
    def test_records_attributes_on_success(self) -> None:
        tracer = _RecordingTracer()
        with Pipeline(_OkClient(), policies=[TracingPolicy(tracer=tracer)]) as p:
            p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
        span = tracer.spans[0]
        assert span.attributes["http.request.method"] == "GET"
        assert span.attributes["http.response.status_code"] == 200
        assert span.attributes["server.address"] == "api.example.com"
        assert span.ended

    def test_records_error_on_exception(self) -> None:
        tracer = _RecordingTracer()
        boom = ServiceRequestError("nope")
        with (
            Pipeline(
                _OkClient(raise_exc=boom),
                policies=[TracingPolicy(tracer=tracer)],
            ) as p,
            pytest.raises(ServiceRequestError),
        ):
            p.run(_request(), DispatchContext(_instr("0" * 16 + "5")))
        span = tracer.spans[0]
        assert span.error_type == "ServiceRequestError"
        assert span.end_error is boom

    def test_records_resend_count_with_retry(self) -> None:
        tracer = _RecordingTracer()
        # Force a failure once via the retry policy.
        client = _OkClient(status=Status.SERVICE_UNAVAILABLE)
        retry = RetryPolicy(status_retries=2, sleep=lambda _: None)
        with Pipeline(
            client,
            policies=[TracingPolicy(tracer=tracer), retry],
        ) as p:
            p.run(_request(), DispatchContext(_instr("0" * 16 + "6")))
        span = tracer.spans[0]
        assert span.attributes.get("http.request.resend_count", 0) >= 1

    def test_opt_out_via_options(self) -> None:
        tracer = _RecordingTracer()
        with Pipeline(_OkClient(), policies=[TracingPolicy(tracer=tracer)]) as p:
            p.run(
                _request(),
                DispatchContext(_instr("0" * 16 + "7")),
                tracing_enabled=False,
            )
        assert tracer.spans == []
