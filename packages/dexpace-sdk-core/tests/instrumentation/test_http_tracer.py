# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the ``HttpTracer`` no-op contract and factory wiring."""

from __future__ import annotations

from dexpace.sdk.core.instrumentation import (
    NOOP_HTTP_TRACER,
    NOOP_HTTP_TRACER_FACTORY,
    NOOP_INSTRUMENTATION_CONTEXT,
    HttpTracer,
    HttpTracerFactory,
)


def test_noop_tracer_callbacks_are_silent_and_return_none() -> None:
    tracer = NOOP_HTTP_TRACER

    # Every callback is declared to return None; calling them must not raise.
    tracer.operation_started()
    tracer.operation_succeeded()
    tracer.operation_failed(RuntimeError("boom"))
    tracer.attempt_started(0)
    tracer.attempt_failed(RuntimeError("boom"), 0.5)
    tracer.attempt_retries_exhausted()
    tracer.request_url_resolved("https://example.test/v1")
    tracer.request_sent(128)
    tracer.request_sent(None)  # unknown body length is a valid argument
    tracer.response_headers_received(200, {"content-type": "application/json"})
    tracer.response_received(256)
    tracer.connection_acquired("example.test", 443)


def test_noop_tracer_is_an_http_tracer() -> None:
    assert isinstance(NOOP_HTTP_TRACER, HttpTracer)


def test_noop_factory_creates_the_shared_noop_tracer() -> None:
    created = NOOP_HTTP_TRACER_FACTORY.create()
    assert created is NOOP_HTTP_TRACER


def test_noop_factory_satisfies_the_factory_protocol() -> None:
    assert isinstance(NOOP_HTTP_TRACER_FACTORY, HttpTracerFactory)


def test_instrumentation_context_defaults_to_noop_factory() -> None:
    assert NOOP_INSTRUMENTATION_CONTEXT.http_tracer_factory is NOOP_HTTP_TRACER_FACTORY


def test_subclass_overrides_only_chosen_events() -> None:
    events: list[tuple[str, object]] = []

    class _RecordingTracer(HttpTracer):
        def attempt_started(self, attempt: int) -> None:
            events.append(("attempt_started", attempt))

        def attempt_failed(self, error: BaseException, next_delay: float) -> None:
            events.append(("attempt_failed", next_delay))

    tracer = _RecordingTracer()
    tracer.operation_started()  # inherited no-op, must not raise
    tracer.attempt_started(2)
    tracer.attempt_failed(ValueError("x"), 1.25)
    tracer.connection_acquired("host", 80)  # inherited no-op

    assert events == [("attempt_started", 2), ("attempt_failed", 1.25)]


def test_custom_factory_is_a_factory() -> None:
    class _Factory:
        def create(self) -> HttpTracer:
            return NOOP_HTTP_TRACER

    assert isinstance(_Factory(), HttpTracerFactory)


def test_request_sent_accepts_known_and_unknown_lengths() -> None:
    received: list[int | None] = []

    class _RecordingTracer(HttpTracer):
        def request_sent(self, byte_count: int | None) -> None:
            received.append(byte_count)

    tracer = _RecordingTracer()
    tracer.request_sent(64)
    tracer.request_sent(None)  # unknown length, e.g. an unsized streamed body

    assert received == [64, None]
