# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the retry-tuning refinements (P6) on ``RetryPolicy``.

Covers the ``X-RateLimit-Reset`` epoch header, full-jitter exponential
backoff, the server ``Retry-After`` ceiling, and the ``HttpTracer`` attempt
events emitted from the retry loop.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import replace

import pytest

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
from dexpace.sdk.core.instrumentation.http_tracer import HttpTracer, HttpTracerFactory
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies import (
    OperationTracingPolicy,
    RetryPolicy,
    TracingPolicy,
)
from dexpace.sdk.core.pipeline.policies.retry import (
    _parse_rate_limit_reset,
    _StatusRetryError,
)

from ..conftest import FakeClock


def _instr(
    trace: str,
    tracer_factory: HttpTracerFactory | None = None,
) -> InstrumentationContext:
    base = InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )
    if tracer_factory is None:
        return base
    return replace(base, http_tracer_factory=tracer_factory)


def _get() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


class _ScriptedClient(HttpClient):
    """Returns one response or raises one error per call, with headers."""

    def __init__(
        self,
        outcomes: Sequence[Status | BaseException],
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._headers = dict(headers or {})
        self.attempts = 0

    def execute(self, request: Request) -> Response:
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        response = Response(request=request, protocol=Protocol.HTTP_1_1, status=outcome)
        if not outcome.is_success:
            for name, value in self._headers.items():
                response = response.with_header(name, value)
        return response


class _RecordingTracer(HttpTracer):
    """Captures every attempt event the retry loop emits."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.failed: list[tuple[BaseException, float]] = []
        self.exhausted = 0

    def attempt_started(self, attempt: int) -> None:
        self.started.append(attempt)

    def attempt_failed(self, error: BaseException, next_delay: float) -> None:
        self.failed.append((error, next_delay))

    def attempt_retries_exhausted(self) -> None:
        self.exhausted += 1


class _RecordingTracerFactory:
    def __init__(self, tracer: HttpTracer) -> None:
        self._tracer = tracer

    def create(self) -> HttpTracer:
        return self._tracer


# ----- X-RateLimit-Reset --------------------------------------------------


class TestRateLimitResetParsing:
    def test_epoch_in_future_yields_positive_delay(self) -> None:
        assert _parse_rate_limit_reset("150", now=100.0) == 50.0

    def test_epoch_in_past_floors_at_zero(self) -> None:
        assert _parse_rate_limit_reset("90", now=100.0) == 0.0

    def test_missing_or_blank_returns_none(self) -> None:
        assert _parse_rate_limit_reset(None, now=0.0) is None
        assert _parse_rate_limit_reset("   ", now=0.0) is None

    def test_non_numeric_returns_none(self) -> None:
        assert _parse_rate_limit_reset("soon", now=0.0) is None


class TestRateLimitResetHonored:
    def test_never_wakes_before_reset(self) -> None:
        clock = FakeClock(start=1_000.0)
        client = _ScriptedClient(
            [Status.TOO_MANY_REQUESTS, Status.OK],
            headers={"X-RateLimit-Reset": "1040"},
        )
        # Bottom of the upward jitter band [1.0, 1.1] -> exactly the reset wait.
        retry = RetryPolicy(clock=clock, rand=_FixedRandom(0.0))
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "1")))
        assert response.status is Status.OK
        # 40s until reset, jitter * 1.0 -> waits to the reset instant, never before.
        assert clock.monotonic() == pytest.approx(1_040.0)

    def test_reset_jitter_only_lengthens_the_wait(self) -> None:
        clock = FakeClock(start=1_000.0)
        client = _ScriptedClient(
            [Status.TOO_MANY_REQUESTS, Status.OK],
            headers={"X-RateLimit-Reset": "1040"},
        )
        # Top of the band [1.0, 1.1] -> 40s * 1.1 = 44s, i.e. slightly past reset.
        retry = RetryPolicy(clock=clock, rand=_FixedRandom(1.0))
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "1")))
        assert clock.monotonic() == pytest.approx(1_044.0)

    def test_retry_after_takes_precedence_over_reset(self) -> None:
        clock = FakeClock(start=1_000.0)
        client = _ScriptedClient(
            [Status.TOO_MANY_REQUESTS, Status.OK],
            headers={"Retry-After": "5", "X-RateLimit-Reset": "9999999"},
        )
        retry = RetryPolicy(clock=clock, rand=_FixedRandom(1.0))
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "2")))
        assert clock.monotonic() == pytest.approx(1_005.0)


# ----- full jitter --------------------------------------------------------


class TestFullJitter:
    def test_seeded_full_jitter_is_reproducible(self) -> None:
        # Same seed must produce the same slept duration, and it must land in
        # the full-jitter band [base*0.5, base*1.0].
        def run_once() -> float:
            clock = FakeClock()
            client = _ScriptedClient(
                [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK]
            )
            retry = RetryPolicy(
                backoff_factor=3.0,
                backoff_max=1_000.0,
                clock=clock,
                rand=random.Random(99),
            )
            with Pipeline(client, policies=[retry]) as p:
                p.run(_get(), DispatchContext(_instr("0" * 16 + "3")))
            return clock.monotonic()

        first = run_once()
        second = run_once()
        assert first == pytest.approx(second)
        # attempts==2 -> base = 3.0 * 2**1 = 6.0; full jitter -> [3.0, 6.0].
        assert 3.0 <= first <= 6.0

    def test_full_jitter_stays_within_band(self) -> None:
        clock = FakeClock()
        rng = random.Random(7)
        client = _ScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK]
        )
        retry = RetryPolicy(backoff_factor=4.0, backoff_max=1_000.0, clock=clock, rand=rng)
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "4")))
        # Single non-zero sleep: attempts==2 -> base = 4.0 * 2 = 8.0.
        # Full jitter keeps it in [4.0, 8.0].
        slept = clock.monotonic()
        assert 4.0 <= slept <= 8.0

    def test_full_jitter_disabled_uses_symmetric_band(self) -> None:
        clock = FakeClock()
        client = _ScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK]
        )
        retry = RetryPolicy(
            backoff_factor=2.0,
            backoff_max=1_000.0,
            full_jitter=False,
            jitter=0.0,
            clock=clock,
        )
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "5")))
        # Deterministic: attempts==2 -> 2.0 * 2**1 = 4.0, no jitter.
        assert clock.monotonic() == pytest.approx(4.0)


# ----- Retry-After ceiling ------------------------------------------------


class TestRetryAfterCeiling:
    def test_caps_outrageous_retry_after(self) -> None:
        clock = FakeClock()
        client = _ScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.OK],
            headers={"Retry-After": "999999"},
        )
        retry = RetryPolicy(retry_after_max=30.0, clock=clock)
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "6")))
        assert clock.monotonic() == pytest.approx(30.0)

    def test_caps_outrageous_rate_limit_reset(self) -> None:
        clock = FakeClock(start=0.0)
        client = _ScriptedClient(
            [Status.TOO_MANY_REQUESTS, Status.OK],
            headers={"X-RateLimit-Reset": "999999"},
        )
        retry = RetryPolicy(retry_after_max=45.0, clock=clock, rand=_FixedRandom(1.0))
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "7")))
        assert clock.monotonic() == pytest.approx(45.0)

    def test_small_retry_after_not_capped(self) -> None:
        clock = FakeClock()
        client = _ScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.OK],
            headers={"Retry-After": "3"},
        )
        retry = RetryPolicy(retry_after_max=3600.0, clock=clock)
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "8")))
        assert clock.monotonic() == pytest.approx(3.0)


# ----- tracer attempt events ----------------------------------------------


class TestTracerAttemptEvents:
    def test_emits_started_and_failed_for_each_retry(self) -> None:
        tracer = _RecordingTracer()
        factory = _RecordingTracerFactory(tracer)
        clock = FakeClock()
        client = _ScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK]
        )
        retry = RetryPolicy(clock=clock, rand=_FixedRandom(0.5))
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "9", factory)))
        # Three attempts started (0, 1, 2); two failed before the success.
        assert tracer.started == [0, 1, 2]
        assert len(tracer.failed) == 2
        assert all(isinstance(err, _StatusRetryError) for err, _ in tracer.failed)
        assert tracer.exhausted == 0

    def test_emits_retries_exhausted_on_budget_exhaustion(self) -> None:
        tracer = _RecordingTracer()
        factory = _RecordingTracerFactory(tracer)
        clock = FakeClock()
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE] * 5)
        retry = RetryPolicy(status_retries=1, clock=clock, rand=_FixedRandom(0.5))
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("a" * 16, factory)))
        assert tracer.exhausted == 1

    def test_status_retry_marker_carries_status_code(self) -> None:
        tracer = _RecordingTracer()
        factory = _RecordingTracerFactory(tracer)
        clock = FakeClock()
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = RetryPolicy(clock=clock, rand=_FixedRandom(0.5))
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("b" * 16, factory)))
        err, _ = tracer.failed[0]
        assert isinstance(err, _StatusRetryError)
        assert err.status == int(Status.SERVICE_UNAVAILABLE)


class _LifecycleTracer(HttpTracer):
    """Records the operation- and attempt-level events on one instance."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def operation_started(self) -> None:
        self.events.append("operation_started")

    def operation_succeeded(self) -> None:
        self.events.append("operation_succeeded")

    def attempt_started(self, attempt: int) -> None:
        self.events.append(f"attempt_started:{attempt}")


class _CountingFactory:
    """Mints a *fresh* tracer per ``create`` — the spec-conformant contract."""

    def __init__(self) -> None:
        self.created: list[_LifecycleTracer] = []

    def create(self) -> HttpTracer:
        tracer = _LifecycleTracer()
        self.created.append(tracer)
        return tracer


class TestSharedTracerAcrossPolicies:
    def test_retry_and_tracing_share_one_per_operation_tracer(self) -> None:
        # With a factory that mints a fresh tracer per ``create`` (the
        # documented contract), the operation-tracing, tracing, and retry
        # policies must still land on a single per-operation instance via the
        # ``ctx.data`` cache — otherwise attempt events and lifecycle events
        # split across objects.
        factory = _CountingFactory()
        clock = FakeClock()
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        with Pipeline(
            client,
            policies=[
                OperationTracingPolicy(),
                TracingPolicy(),
                RetryPolicy(clock=clock, rand=_FixedRandom(0.5)),
            ],
        ) as p:
            p.run(_get(), DispatchContext(_instr("c" * 16, factory)))
        assert len(factory.created) == 1
        tracer = factory.created[0]
        assert "operation_started" in tracer.events
        assert "attempt_started:0" in tracer.events
        assert "attempt_started:1" in tracer.events
        assert "operation_succeeded" in tracer.events


class _FixedRandom(random.Random):
    """``random.Random`` whose ``uniform`` always returns a fixed factor."""

    def __init__(self, factor: float) -> None:
        super().__init__()
        self._factor = factor

    def uniform(self, a: float, b: float) -> float:
        return b * self._factor if self._factor == 1.0 else a + (b - a) * self._factor
