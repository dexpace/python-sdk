# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

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
from dexpace.sdk.core.http.request import (
    LoggableRequestBody,
    Method,
    Request,
    RequestBody,
)
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


class _CapturingClient(HttpClient):
    """Transport that drains and records the full request body it receives."""

    def __init__(self) -> None:
        self.received = bytearray()

    def execute(self, request: Request) -> Response:
        if request.body is not None:
            for chunk in request.body.iter_bytes():
                self.received.extend(chunk)
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


def _post(body: RequestBody) -> Request:
    return Request(
        method=Method.POST,
        url=Url.parse("https://api.example.com/v1"),
        body=body,
    )


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

    @pytest.mark.req("BODY-34")
    def test_opt_out_via_options(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(_OkClient(), policies=[LoggingPolicy()]) as p:
            p.run(
                _request(),
                DispatchContext(_instr("0" * 16 + "3")),
                logging_enabled=False,
            )
        assert not caplog.records

    @pytest.mark.req("XCUT-19")
    def test_default_preview_size_is_8_kib(self) -> None:
        # The logging policy defaults to a small, bounded 8 KiB body preview.
        assert LoggingPolicy().preview_bytes == 8 * 1024

    def test_preview_bytes_is_configurable(self) -> None:
        assert LoggingPolicy(preview_bytes=256).preview_bytes == 256

    def test_preview_bytes_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            LoggingPolicy(preview_bytes=0)

    @pytest.mark.req("BODY-34", "OBS-36", "XCUT-24")
    def test_full_body_forwarded_uncut_while_preview_truncates(
        self, caplog: LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = b"x" * (8 * 1024 + 500)  # larger than the 8 KiB preview cap
        client = _CapturingClient()
        with Pipeline(client, policies=[LoggingPolicy()]) as p:
            p.run(_post(RequestBody.from_bytes(payload)), DispatchContext(_instr("0" * 16 + "8")))
        # The transport received every byte — only the diagnostic tap truncates.
        assert bytes(client.received) == payload
        # The emitted preview is capped at the 8 KiB default.
        responses = [m for m in (r.getMessage() for r in caplog.records) if "http.response" in m]
        assert responses
        preview = responses[0].split("request_body_preview=")[1]
        assert len(preview) == 8 * 1024

    @pytest.mark.req("OBS-38")
    def test_binary_body_preview_never_throws(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = bytes(range(256)) * 40  # invalid UTF-8 sequences, > 8 KiB
        client = _CapturingClient()
        with Pipeline(client, policies=[LoggingPolicy()]) as p:
            p.run(_post(RequestBody.from_bytes(payload)), DispatchContext(_instr("0" * 16 + "9")))
        assert bytes(client.received) == payload
        assert any("http.response" in r.getMessage() for r in caplog.records)

    def test_preview_bounded_even_when_wrapper_ceiling_is_larger(
        self, caplog: LogCaptureFixture
    ) -> None:
        # A caller-supplied wrapper with a large ceiling captures more than the
        # preview, yet the emitted preview is still bounded to the 8 KiB default.
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = b"y" * (16 * 1024)
        prewrapped = LoggableRequestBody(
            RequestBody.from_bytes(payload), max_capture_bytes=32 * 1024
        )
        client = _CapturingClient()
        with Pipeline(client, policies=[LoggingPolicy()]) as p:
            p.run(_post(prewrapped), DispatchContext(_instr("0" * 16 + "a")))
        assert bytes(client.received) == payload
        assert prewrapped.captured_size == 16 * 1024  # tap kept the larger slice
        responses = [m for m in (r.getMessage() for r in caplog.records) if "http.response" in m]
        assert responses
        preview = responses[0].split("request_body_preview=")[1]
        assert len(preview) == 8 * 1024  # preview stays capped

    def test_wrapper_ceiling_independent_of_logging_preview(self) -> None:
        # The wrapper's own default cap stays large and is not lowered to the
        # logging policy's 8 KiB preview default.
        body = LoggableRequestBody(RequestBody.from_bytes(b"z" * (9 * 1024)))
        list(body.iter_bytes())
        assert body.captured_size == 9 * 1024


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
        retry = RetryPolicy(status_retries=2, clock=FakeClock())
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
