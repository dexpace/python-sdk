# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests that ``TracingPolicy`` binds trace/span ids for log correlation (P8).

The policy sets the active trace and span ids in the ``correlation``
``contextvars`` for the duration of the downstream send, so any log record
emitted while the request is in flight carries them. The bindings are scoped:
they restore the prior values once the request completes (success or error).
"""

from __future__ import annotations

import logging
from typing import Any

from _pytest.logging import LogCaptureFixture

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, Status
from dexpace.sdk.core.instrumentation import (
    ClientLogger,
    InstrumentationContext,
    Span,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    Tracer,
    TraceState,
    TracingScope,
    get_span_id,
    get_trace_id,
)
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.context import PipelineContext
from dexpace.sdk.core.pipeline.policies import TracingPolicy
from dexpace.sdk.core.pipeline.policy import Policy
from dexpace.sdk.core.pipeline.stage import Stage

_TRACE = "1" * 32
_SPAN = "2" * 16


def _valid_context() -> InstrumentationContext:
    """Build a recording context with real (non-sentinel) ids."""
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(_TRACE),
        span_id=SpanId(_SPAN),
        span=_RecordingSpan.placeholder(),
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


class _Scope(TracingScope):
    def close(self) -> None:
        return None

    def __enter__(self) -> _Scope:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _RecordingSpan(Span):
    """Span that reports a valid context so the policy binds real ids."""

    def __init__(self) -> None:
        self._ended = False

    @classmethod
    def placeholder(cls) -> _RecordingSpan:
        return cls()

    @property
    def is_recording(self) -> bool:
        return True

    @property
    def context(self) -> InstrumentationContext:
        # A self-referential context would recurse; build a leaf one with the
        # same ids but a no-op span field is not needed here — the policy only
        # reads ``trace_id`` / ``span_id`` / ``is_valid``.
        return InstrumentationContext(
            trace_id_type=TraceIdType.W3C,
            trace_id=TraceId(_TRACE),
            span_id=SpanId(_SPAN),
            span=self,
            trace_flags=TraceFlags.NOOP,
            trace_state=TraceState.NOOP,
        )

    def set_attribute(self, key: str, value: Any) -> _RecordingSpan:
        return self

    def set_error(self, error_type: str) -> _RecordingSpan:
        return self

    def make_current(self) -> TracingScope:
        return _Scope()

    def end(self, error: BaseException | None = None) -> None:
        self._ended = True


class _RecordingTracer(Tracer):
    def start_span(
        self,
        name: str,
        parent: InstrumentationContext | None = None,
    ) -> Span:
        del name, parent
        return _RecordingSpan()


class _CaptureCorrelationPolicy(Policy):
    """Innermost policy that snapshots the bound ids when the request runs."""

    STAGE = Stage.PRE_SEND
    __slots__ = ("seen_span", "seen_trace")

    def __init__(self) -> None:
        self.seen_trace: str | None = None
        self.seen_span: str | None = None

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        self.seen_trace = get_trace_id()
        self.seen_span = get_span_id()
        return self.next.send(request, ctx)


class _OkClient(HttpClient):
    def __init__(self, *, raise_exc: BaseException | None = None) -> None:
        self.raise_exc = raise_exc

    def execute(self, request: Request) -> Response:
        if self.raise_exc is not None:
            raise self.raise_exc
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/v1"))


class TestCorrelationBinding:
    def test_ids_bound_during_request(self) -> None:
        capture = _CaptureCorrelationPolicy()
        with Pipeline(
            _OkClient(),
            policies=[TracingPolicy(tracer=_RecordingTracer()), capture],
        ) as p:
            p.run(_request(), DispatchContext(_valid_context()))
        assert capture.seen_trace == _TRACE
        assert capture.seen_span == _SPAN

    def test_ids_reset_after_request(self) -> None:
        assert get_trace_id() is None
        assert get_span_id() is None
        with Pipeline(
            _OkClient(),
            policies=[TracingPolicy(tracer=_RecordingTracer())],
        ) as p:
            p.run(_request(), DispatchContext(_valid_context()))
        # The scoped binding restored the (unset) prior values.
        assert get_trace_id() is None
        assert get_span_id() is None

    def test_ids_reset_after_exception(self) -> None:
        boom = ServiceRequestError("connect failed")
        raised: BaseException | None = None
        with Pipeline(
            _OkClient(raise_exc=boom),
            policies=[TracingPolicy(tracer=_RecordingTracer())],
        ) as p:
            try:
                p.run(_request(), DispatchContext(_valid_context()))
            except ServiceRequestError as err:
                raised = err
        assert raised is boom
        assert get_trace_id() is None
        assert get_span_id() is None

    def test_noop_span_binds_no_ids(self) -> None:
        # A context whose span carries sentinel ids must not bind a fake trace.
        capture = _CaptureCorrelationPolicy()
        with Pipeline(
            _OkClient(),
            # No tracer -> NOOP_TRACER -> NOOP_SPAN (is_valid is False).
            policies=[TracingPolicy(), capture],
        ) as p:
            p.run(_request(), DispatchContext(_valid_context()))
        assert capture.seen_trace is None
        assert capture.seen_span is None


class TestLogRecordCorrelation:
    def test_log_record_carries_bound_ids(self, caplog: LogCaptureFixture) -> None:
        logger = ClientLogger("dexpace.sdk.core.test.correlation")

        class _LoggingPolicy(Policy):
            STAGE = Stage.PRE_SEND
            __slots__ = ()

            def send(self, request: Request, ctx: PipelineContext) -> Response:
                logger.info("in.flight")
                return self.next.send(request, ctx)

        caplog.set_level(logging.INFO, logger="dexpace.sdk.core.test.correlation")
        with Pipeline(
            _OkClient(),
            policies=[TracingPolicy(tracer=_RecordingTracer()), _LoggingPolicy()],
        ) as p:
            p.run(_request(), DispatchContext(_valid_context()))
        records = [r for r in caplog.records if r.getMessage().startswith("in.flight")]
        assert records, "expected an in-flight log record"
        record = records[0]
        assert getattr(record, "trace.id") == _TRACE
        assert getattr(record, "span.id") == _SPAN
