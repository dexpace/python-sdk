# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncLoggingPolicy`` and ``AsyncTracingPolicy``.

Async twin of ``test_observability_policies.py``. Verifies the async pillar
emits the same wire-log event model as the sync policy, previews the request
body (reusing the sync ``LoggableRequestBody`` tap) and — via the async
``AsyncLoggableResponseBody`` twin — the response body, and drives the same
per-attempt tracing span / ``HttpTracer`` callbacks the sync ``TracingPolicy``
does. It also pins the async-specific behaviours the sync side lacks:
capture is skipped for a response whose content length is undeclared, and the
tracing span / metrics fire independently of whether logging is enabled.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from _pytest.logging import LogCaptureFixture

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.media_type import MediaType
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, AsyncResponseBody, Status
from dexpace.sdk.core.instrumentation import (
    HttpTracer,
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    Tracer,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import AsyncPipeline
from dexpace.sdk.core.pipeline.policies import AsyncRetryPolicy, HttpLogDetailLevel
from dexpace.sdk.core.pipeline.policies.async_logging_policy import AsyncLoggingPolicy
from dexpace.sdk.core.pipeline.policies.async_tracing_policy import AsyncTracingPolicy


def _instr(trace: str, *, tracer_factory: Any | None = None) -> InstrumentationContext:
    kwargs: dict[str, Any] = {}
    if tracer_factory is not None:
        kwargs["http_tracer_factory"] = tracer_factory
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
        **kwargs,
    )


def _request(url: str = "https://api.example.com/v1?token=secret&api-version=1.0") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


def _post(body: RequestBody) -> Request:
    return Request(method=Method.POST, url=Url.parse("https://api.example.com/v1"), body=body)


class _UnknownLengthResponseBody(AsyncResponseBody):
    """Async response body whose declared content length is unknown (-1)."""

    def __init__(self, data: bytes, media_type: MediaType | None = None) -> None:
        self._data = data
        self._media_type = media_type
        self._closed = False

    def media_type(self) -> MediaType | None:
        return self._media_type

    def content_length(self) -> int:
        return -1

    def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        return self._aiter(chunk_size)

    async def _aiter(self, chunk_size: int) -> AsyncIterator[bytes]:
        view = memoryview(self._data)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])
        await self.close()

    async def close(self) -> None:
        self._closed = True


class _StubAsyncClient(AsyncHttpClient):
    """Async transport: drains the request body and returns a fresh response."""

    def __init__(
        self,
        *,
        status: Status = Status.OK,
        raise_exc: BaseException | None = None,
        body_factory: Any | None = None,
        headers: Headers | None = None,
    ) -> None:
        self.status = status
        self.raise_exc = raise_exc
        self.body_factory = body_factory
        self.headers = headers or Headers()
        self.received = bytearray()
        self.calls = 0

    async def execute(self, request: Request) -> AsyncResponse:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        if request.body is not None:
            for chunk in request.body.iter_bytes():
                self.received.extend(chunk)
        body = self.body_factory() if self.body_factory is not None else None
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=self.status,
            headers=self.headers,
            body=body,
        )


def _messages(caplog: LogCaptureFixture) -> list[str]:
    return [rec.getMessage() for rec in caplog.records]


class TestAsyncLoggingPolicy:
    async def test_emits_request_and_response(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        async with AsyncPipeline(
            _StubAsyncClient(),
            policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)],
        ) as p:
            await p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
        messages = _messages(caplog)
        assert any("http.request" in m for m in messages)
        assert any("http.response" in m for m in messages)
        joined = " ".join(messages)
        assert "secret" not in joined
        assert "REDACTED" in joined

    @pytest.mark.req("OBS-39")
    async def test_logs_error_on_exception(self, caplog: LogCaptureFixture) -> None:
        # Parity with the sync policy: a failure emits an ``http.response`` event
        # carrying ``error.type`` and the throwable cause.
        caplog.set_level(logging.ERROR, logger="dexpace.sdk.core.http")
        boom = ServiceRequestError("connect failed")
        async with AsyncPipeline(
            _StubAsyncClient(raise_exc=boom),
            policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)],
        ) as p:
            with pytest.raises(ServiceRequestError):
                await p.run(_request(), DispatchContext(_instr("0" * 16 + "2")))
        assert any(
            m.split()[0] == "http.response"
            and "error.type=ServiceRequestError" in m
            and "connect failed" in m
            for m in _messages(caplog)
        )

    async def test_opt_out_via_options(self, caplog: LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        async with AsyncPipeline(
            _StubAsyncClient(),
            policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.BODY)],
        ) as p:
            await p.run(
                _request(),
                DispatchContext(_instr("0" * 16 + "3")),
                logging_enabled=False,
            )
        assert not caplog.records

    def test_default_preview_size_is_8_kib(self) -> None:
        assert AsyncLoggingPolicy().preview_bytes == 8 * 1024

    def test_preview_bytes_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            AsyncLoggingPolicy(preview_bytes=0)

    async def test_full_request_body_forwarded_uncut_while_preview_truncates(
        self, caplog: LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = b"x" * (8 * 1024 + 500)
        client = _StubAsyncClient()
        async with AsyncPipeline(
            client, policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]
        ) as p:
            await p.run(
                _post(RequestBody.from_bytes(payload)), DispatchContext(_instr("0" * 16 + "8"))
            )
        # The transport received every byte — only the diagnostic tap truncates.
        assert bytes(client.received) == payload
        responses = [m for m in _messages(caplog) if "http.response" in m]
        assert responses
        preview = responses[0].split("request_body_preview=")[1].split()[0]
        assert len(preview) == 8 * 1024

    async def test_binary_request_body_preview_never_throws(
        self, caplog: LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = bytes(range(256)) * 40  # invalid UTF-8 sequences, > 8 KiB
        client = _StubAsyncClient()
        async with AsyncPipeline(
            client, policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]
        ) as p:
            await p.run(
                _post(RequestBody.from_bytes(payload)), DispatchContext(_instr("0" * 16 + "9"))
            )
        assert bytes(client.received) == payload
        assert any("http.response" in m for m in _messages(caplog))

    async def test_response_body_preview_captured_when_length_known(
        self, caplog: LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = b'{"ok":true}'
        client = _StubAsyncClient(body_factory=lambda: AsyncResponseBody.from_bytes(payload))
        async with AsyncPipeline(
            client, policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]
        ) as p:
            response = await p.run(_request(), DispatchContext(_instr("0" * 16 + "a")))
            # The full body is still available to the caller after previewing.
            assert await response.body.bytes() == payload  # type: ignore[union-attr]
        responses = [m for m in _messages(caplog) if "http.response" in m]
        assert responses
        assert "response_body_preview=" in responses[0]

    @pytest.mark.req("OBS-36")
    async def test_response_full_body_forwarded_while_preview_truncates(
        self, caplog: LogCaptureFixture
    ) -> None:
        # A known-length response larger than the preview cap: the caller still
        # reads every byte (prefix + streamed tail), only the preview truncates.
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = b"R" * 1024
        client = _StubAsyncClient(body_factory=lambda: AsyncResponseBody.from_bytes(payload))
        async with AsyncPipeline(
            client,
            policies=[AsyncLoggingPolicy(preview_bytes=256, detail_level=HttpLogDetailLevel.BODY)],
        ) as p:
            response = await p.run(_request(), DispatchContext(_instr("0" * 15 + "a1")))
            assert await response.body.bytes() == payload  # type: ignore[union-attr]
        responses = [m for m in _messages(caplog) if "http.response" in m]
        assert responses
        preview = responses[0].split("response_body_preview=")[1].split()[0]
        assert len(preview) == 256

    @pytest.mark.req("OBS-37")
    async def test_response_capture_skipped_for_unknown_length(
        self, caplog: LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = b"streamed-unknown-length-body"
        client = _StubAsyncClient(body_factory=lambda: _UnknownLengthResponseBody(payload))
        async with AsyncPipeline(
            client, policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]
        ) as p:
            response = await p.run(_request(), DispatchContext(_instr("0" * 16 + "b")))
            # The body is passed through untouched; the caller drains it in full.
            assert await response.body.bytes() == payload  # type: ignore[union-attr]
        responses = [m for m in _messages(caplog) if "http.response" in m]
        assert responses
        # No preview for a body whose size was not declared.
        assert "response_body_preview=" not in responses[0]

    @pytest.mark.req("OBS-17")
    async def test_url_valued_response_header_is_redacted(self, caplog: LogCaptureFixture) -> None:
        # Parity with the sync policy via the shared HeaderRedactor: a Location
        # secret is redacted and a credential header value is never logged.
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        headers = Headers.outbound(
            [
                ("Location", "https://idp.example.com/cb?code=SEKRET"),
                ("Authorization", "Bearer super-secret-token"),
            ]
        )
        client = _StubAsyncClient(headers=headers)
        async with AsyncPipeline(
            client, policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)]
        ) as p:
            await p.run(_request(), DispatchContext(_instr("0" * 15 + "d2")))
        response_event = next(m for m in _messages(caplog) if m.split()[0] == "http.response")
        assert "SEKRET" not in response_event
        assert "super-secret-token" not in response_event
        assert "http.response.header.location=" in response_event
        assert "http.response.header.authorization=REDACTED" in response_event

    @pytest.mark.req("OBS-38")
    async def test_binary_response_body_preview_never_throws(
        self, caplog: LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        payload = bytes(range(256))  # invalid UTF-8, known length
        client = _StubAsyncClient(body_factory=lambda: AsyncResponseBody.from_bytes(payload))
        async with AsyncPipeline(
            client, policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.BODY)]
        ) as p:
            response = await p.run(_request(), DispatchContext(_instr("0" * 16 + "c")))
            assert await response.body.bytes() == payload  # type: ignore[union-attr]
        assert any("http.response" in m for m in _messages(caplog))


class _RecordingHttpTracer(HttpTracer):
    def __init__(self) -> None:
        self.events: list[str] = []

    def operation_started(self) -> None:
        self.events.append("operation_started")

    def operation_succeeded(self) -> None:
        self.events.append("operation_succeeded")

    def operation_failed(self, error: BaseException) -> None:
        self.events.append("operation_failed")

    def request_sent(self, byte_count: int | None) -> None:
        self.events.append("request_sent")

    def response_headers_received(self, status: int, headers: Mapping[str, str]) -> None:
        self.events.append("response_headers_received")

    def response_received(self, byte_count: int) -> None:
        self.events.append("response_received")


class _RecordingTracerFactory:
    def __init__(self, tracer: HttpTracer) -> None:
        self._tracer = tracer

    def create(self) -> HttpTracer:
        return self._tracer


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.error_type: str | None = None
        self.ended = False
        self.end_error: BaseException | None = None

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
        return _RecordingScope()

    def end(self, error: BaseException | None = None) -> None:
        self.ended = True
        self.end_error = error


class _RecordingScope:
    def __enter__(self) -> _RecordingScope:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass


class _RecordingTracer(Tracer):
    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    def start_span(self, name: str, parent: InstrumentationContext | None = None) -> Any:
        del name, parent
        span = _RecordingSpan()
        self.spans.append(span)
        return span


class TestAsyncTracingPolicy:
    async def test_records_attributes_on_success(self) -> None:
        tracer = _RecordingTracer()
        async with AsyncPipeline(
            _StubAsyncClient(), policies=[AsyncTracingPolicy(tracer=tracer)]
        ) as p:
            await p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
        span = tracer.spans[0]
        assert span.attributes["http.request.method"] == "GET"
        assert span.attributes["http.response.status_code"] == 200
        assert span.attributes["server.address"] == "api.example.com"
        assert span.ended

    async def test_records_error_on_exception(self) -> None:
        tracer = _RecordingTracer()
        boom = ServiceRequestError("nope")
        async with AsyncPipeline(
            _StubAsyncClient(raise_exc=boom), policies=[AsyncTracingPolicy(tracer=tracer)]
        ) as p:
            with pytest.raises(ServiceRequestError):
                await p.run(_request(), DispatchContext(_instr("0" * 16 + "5")))
        span = tracer.spans[0]
        assert span.error_type == "ServiceRequestError"
        assert span.end_error is boom

    async def test_records_resend_count_with_retry(self) -> None:
        from .test_async_pipeline import _AsyncFakeClock

        tracer = _RecordingTracer()

        class _ScriptedClient(AsyncHttpClient):
            def __init__(self) -> None:
                self.statuses = iter([Status.SERVICE_UNAVAILABLE, Status.OK])

            async def execute(self, request: Request) -> AsyncResponse:
                return AsyncResponse(
                    request=request, protocol=Protocol.HTTP_1_1, status=next(self.statuses)
                )

        retry = AsyncRetryPolicy(status_retries=2, backoff_factor=0, clock=_AsyncFakeClock())
        async with AsyncPipeline(
            _ScriptedClient(), policies=[AsyncTracingPolicy(tracer=tracer), retry]
        ) as p:
            await p.run(_request(), DispatchContext(_instr("0" * 16 + "6")))
        span = tracer.spans[0]
        assert span.attributes.get("http.request.resend_count", 0) >= 1

    async def test_opt_out_via_options(self) -> None:
        tracer = _RecordingTracer()
        async with AsyncPipeline(
            _StubAsyncClient(), policies=[AsyncTracingPolicy(tracer=tracer)]
        ) as p:
            await p.run(
                _request(),
                DispatchContext(_instr("0" * 16 + "7")),
                tracing_enabled=False,
            )
        assert tracer.spans == []


class TestLevelIndependence:
    @pytest.mark.req("OBS-34")
    async def test_span_and_tracer_events_fire_when_logging_disabled(
        self, caplog: LogCaptureFixture
    ) -> None:
        # Span emission and HttpTracer (metrics-driving) callbacks are owned by
        # the tracing policy, not gated by the logging policy's enabled state:
        # disabling logging must not silence tracing or metrics.
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        http_tracer = _RecordingHttpTracer()
        span_tracer = _RecordingTracer()
        instr = _instr("0" * 16 + "e", tracer_factory=_RecordingTracerFactory(http_tracer))
        async with AsyncPipeline(
            _StubAsyncClient(),
            policies=[
                AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.BODY),
                AsyncTracingPolicy(tracer=span_tracer),
            ],
        ) as p:
            await p.run(_request(), DispatchContext(instr), logging_enabled=False)
        # No wire logs (logging disabled) ...
        assert not any("http.request" in m or "http.response" in m for m in _messages(caplog))
        # ... yet the span opened and closed, and the per-request tracer events fired.
        assert span_tracer.spans and span_tracer.spans[0].ended
        assert "request_sent" in http_tracer.events
        assert "response_received" in http_tracer.events

    @pytest.mark.req("OBS-34")
    async def test_default_detail_level_is_none(self, caplog: LogCaptureFixture) -> None:
        # Security default: a policy built with no explicit ``detail_level``
        # defaults to NONE, so merely raising the logger to INFO does not leak
        # request/response events (or their body previews) without an explicit
        # opt-in. An explicit BODY level still emits both events.
        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        async with AsyncPipeline(_StubAsyncClient(), policies=[AsyncLoggingPolicy()]) as p:
            await p.run(_request(), DispatchContext(_instr("0" * 15 + "e4")))
        assert not any("http.request" in m or "http.response" in m for m in _messages(caplog))
        caplog.clear()
        async with AsyncPipeline(
            _StubAsyncClient(),
            policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.BODY)],
        ) as p:
            await p.run(_request(), DispatchContext(_instr("0" * 15 + "e5")))
        messages = _messages(caplog)
        assert any("http.request" in m for m in messages)
        assert any("http.response" in m for m in messages)


def _event_names(caplog: LogCaptureFixture) -> set[str]:
    return {m.split()[0] for m in _messages(caplog) if m.split()[0].startswith("http.")}


class TestEventNameParity:
    async def test_async_and_sync_emit_the_same_event_names(
        self, caplog: LogCaptureFixture
    ) -> None:
        # For an identical GET-then-200 scenario, both pillars must emit the same
        # wire-log event names so a log consumer sees one naming across sync/async.
        from dexpace.sdk.core.client.http_client import HttpClient
        from dexpace.sdk.core.http.response import Response
        from dexpace.sdk.core.pipeline import Pipeline
        from dexpace.sdk.core.pipeline.policies import LoggingPolicy

        class _SyncOkClient(HttpClient):
            def execute(self, request: Request) -> Response:
                return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)

        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        with Pipeline(
            _SyncOkClient(), policies=[LoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)]
        ) as sp:
            sp.run(_request(), DispatchContext(_instr("0" * 15 + "11")))
        sync_events = _event_names(caplog)
        caplog.clear()
        async with AsyncPipeline(
            _StubAsyncClient(),
            policies=[AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS)],
        ) as ap:
            await ap.run(_request(), DispatchContext(_instr("0" * 15 + "12")))
        async_events = _event_names(caplog)
        assert async_events == sync_events == {"http.request", "http.response"}


class TestPerAttemptLogging:
    async def test_wire_logs_are_emitted_per_attempt_inside_retry(
        self, caplog: LogCaptureFixture
    ) -> None:
        # Logging sits inside the retry wrapper in the default async stack, so a
        # request that retries once produces two request/response log pairs.
        from dexpace.sdk.core.pipeline.defaults import default_async_pipeline

        from .test_async_pipeline import _AsyncFakeClock

        class _ScriptedClient(AsyncHttpClient):
            def __init__(self) -> None:
                self.statuses = iter([Status.SERVICE_UNAVAILABLE, Status.OK])

            async def execute(self, request: Request) -> AsyncResponse:
                return AsyncResponse(
                    request=request, protocol=Protocol.HTTP_1_1, status=next(self.statuses)
                )

        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.http")
        retry = AsyncRetryPolicy(backoff_factor=0, clock=_AsyncFakeClock())
        pipeline = default_async_pipeline(
            _ScriptedClient(),
            retry=retry,
            logging=AsyncLoggingPolicy(detail_level=HttpLogDetailLevel.HEADERS),
        ).build()
        async with pipeline as p:
            await p.run(_request(), DispatchContext(_instr("0" * 15 + "13")))
        messages = _messages(caplog)
        requests = [m for m in messages if m.split()[0] == "http.request"]
        responses = [m for m in messages if m.split()[0] == "http.response"]
        assert len(requests) == 2  # one per attempt, not one per call
        assert len(responses) == 2
