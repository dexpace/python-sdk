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
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import (
    LoggableRequestBody,
    Method,
    Request,
    RequestBody,
)
from dexpace.sdk.core.http.response import Response, Status
from dexpace.sdk.core.instrumentation import (
    HeaderRedactor,
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
from dexpace.sdk.core.pipeline.policies import (
    HttpLogDetailLevel,
    LoggingPolicy,
    RetryPolicy,
    TracingPolicy,
)

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
        self,
        *,
        status: Status = Status.OK,
        raise_exc: BaseException | None = None,
        headers: Headers | None = None,
    ) -> None:
        self.status = status
        self.raise_exc = raise_exc
        self.headers = headers or Headers()

    def execute(self, request: Request) -> Response:
        if self.raise_exc is not None:
            raise self.raise_exc
        return Response(
            request=request, protocol=Protocol.HTTP_1_1, status=self.status, headers=self.headers
        )


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
        with Pipeline(
            _OkClient(), policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)]
        ) as p:
            p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("http.request" in m for m in messages)
        assert any("http.response" in m for m in messages)
        # URL is redacted — token value must not appear in the log.
        joined = " ".join(messages)
        assert "secret" not in joined
        assert "REDACTED" in joined

    @pytest.mark.req("OBS-39")
    def test_logs_error_on_exception(self, caplog: LogCaptureFixture) -> None:
        # A failure emits an ``http.response`` event carrying ``error.type`` and
        # the throwable cause, not a separate error event name.
        caplog.set_level(logging.ERROR, logger="dexpace.sdk.core.http")
        boom = ServiceRequestError("connect failed")
        with (
            Pipeline(
                _OkClient(raise_exc=boom),
                policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)],
            ) as p,
            pytest.raises(ServiceRequestError),
        ):
            p.run(_request(), DispatchContext(_instr("0" * 16 + "2")))
        assert any(
            m.split()[0] == "http.response"
            and "error.type=ServiceRequestError" in m
            and "connect failed" in m
            for m in (rec.getMessage() for rec in caplog.records)
        )

    @pytest.mark.req("BODY-34")
    def test_opt_out_via_options(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(
            _OkClient(), policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]
        ) as p:
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
        with Pipeline(client, policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]) as p:
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
        with Pipeline(client, policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]) as p:
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
        with Pipeline(client, policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]) as p:
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


def _response_events(caplog: LogCaptureFixture) -> list[str]:
    return [m for m in (r.getMessage() for r in caplog.records) if m.split()[0] == "http.response"]


def _request_events(caplog: LogCaptureFixture) -> list[str]:
    return [m for m in (r.getMessage() for r in caplog.records) if m.split()[0] == "http.request"]


class TestLogDetailLevel:
    """OBS-34: selectable none / headers / body granularity."""

    @pytest.mark.req("OBS-34")
    def test_none_emits_no_events(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        policy = LoggingPolicy(detail_level=HttpLogDetailLevel.NONE)
        with Pipeline(_OkClient(), policies=[policy]) as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "b0")))
        # Even with the logger explicitly enabled, NONE emits nothing.
        assert not any(
            m.split()[0].startswith("http.") for m in (r.getMessage() for r in caplog.records)
        )

    @pytest.mark.req("OBS-34")
    def test_default_detail_level_is_none(self, caplog: LogCaptureFixture) -> None:
        # Security default: a policy built with no explicit ``detail_level``
        # defaults to NONE, so merely raising the logger to INFO does not leak
        # request/response events (or their body previews) without an explicit
        # opt-in. An explicit BODY level still emits both events.
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(_OkClient(), policies=[LoggingPolicy()]) as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "b4")))
        assert not _request_events(caplog) and not _response_events(caplog)
        caplog.clear()
        with Pipeline(
            _OkClient(), policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]
        ) as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "b5")))
        assert _request_events(caplog) and _response_events(caplog)

    @pytest.mark.req("OBS-34")
    def test_headers_level_omits_body_preview(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = b"x" * 4096
        client = _CapturingClient()
        policy = LoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)
        with Pipeline(client, policies=[policy]) as p:
            p.run(_post(RequestBody.from_bytes(payload)), DispatchContext(_instr("0" * 15 + "b1")))
        # Events fire, but body capture happens only at BODY level.
        assert _request_events(caplog) and _response_events(caplog)
        assert "request_body_preview=" not in _response_events(caplog)[0]
        # The transport still received the full body untouched.
        assert bytes(client.received) == payload

    @pytest.mark.req("OBS-34")
    def test_body_level_captures_body_preview(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        client = _CapturingClient()
        policy = LoggingPolicy(detail_level=HttpLogDetailLevel.BODY)
        with Pipeline(client, policies=[policy]) as p:
            p.run(_post(RequestBody.from_bytes(b"body")), DispatchContext(_instr("0" * 15 + "b2")))
        assert "request_body_preview=" in _response_events(caplog)[0]

    @pytest.mark.req("OBS-34")
    def test_none_silences_logs_but_tracing_still_records(self, caplog: LogCaptureFixture) -> None:
        # Span lifecycle runs on every request independent of the log level:
        # NONE silences log events without disabling tracing.
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        tracer = _RecordingTracer()
        policies = [
            TracingPolicy(tracer=tracer),
            LoggingPolicy(detail_level=HttpLogDetailLevel.NONE),
        ]
        with Pipeline(_OkClient(), policies=policies) as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "b3")))
        assert not _request_events(caplog) and not _response_events(caplog)
        assert tracer.spans and tracer.spans[0].ended


class TestNormativeFieldVocabulary:
    """OBS-39: stable event names and OTel-style field keys."""

    @pytest.mark.req("OBS-39")
    def test_success_events_use_normative_field_keys(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(
            _OkClient(), policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)]
        ) as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "c0")))
        request_event = _request_events(caplog)[0]
        response_event = _response_events(caplog)[0]
        assert "http.request.method=GET" in request_event
        assert "url.full=" in request_event
        assert "http.request.method=GET" in response_event
        assert "url.full=" in response_event
        assert "http.response.status_code=200" in response_event
        assert "http.response.duration_ms=" in response_event

    @pytest.mark.req("OBS-39")
    def test_url_full_is_always_redacted(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(
            _OkClient(), policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)]
        ) as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "c1")))
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "secret" not in joined
        assert "url.full=" in joined and "REDACTED" in joined


class TestHeaderLogging:
    """OBS-17/18: response-header logging, URL-value redaction, name allow-list."""

    def _client(self) -> _OkClient:
        headers = Headers.outbound(
            [
                ("Location", "https://idp.example.com/cb?code=SEKRET&api-version=1"),
                ("Content-Type", "application/json"),
                ("Authorization", "Bearer super-secret-token"),
            ]
        )
        return _OkClient(headers=headers)

    @pytest.mark.req("OBS-17")
    def test_url_valued_response_header_is_redacted(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(
            self._client(), policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)]
        ) as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "d0")))
        response_event = _response_events(caplog)[0]
        assert "SEKRET" not in response_event
        assert "http.response.header.location=" in response_event
        assert "REDACTED=REDACTED" in response_event

    @pytest.mark.req("OBS-18")
    def test_credential_header_value_is_never_logged(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(
            self._client(), policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)]
        ) as p:
            p.run(_request(), DispatchContext(_instr("0" * 15 + "d1")))
        response_event = _response_events(caplog)[0]
        assert "super-secret-token" not in response_event
        assert "http.response.header.authorization=REDACTED" in response_event
        assert "http.response.header.content-type=application/json" in response_event

    @pytest.mark.req("OBS-17")
    def test_header_redactor_is_shared_injectable_into_both_pillars(self) -> None:
        from dexpace.sdk.core.pipeline.policies.async_logging_policy import AsyncLoggingPolicy

        shared = HeaderRedactor()
        sync_policy = LoggingPolicy(header_redactor=shared)
        async_policy = AsyncLoggingPolicy(header_redactor=shared)
        # One redaction policy, wired into both pillars, so it cannot drift.
        assert sync_policy._header_redactor is shared
        assert async_policy._header_redactor is shared


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
