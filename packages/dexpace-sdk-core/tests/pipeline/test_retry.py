# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``RetryPolicy`` behaviour."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Sequence
from typing import Any

import pytest

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import (
    ClientAuthenticationError,
    SdkError,
    ServiceRequestError,
    ServiceResponseError,
)
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
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
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline._pure.classifier import DEFAULT_RETRYABLE_STATUS
from dexpace.sdk.core.pipeline._pure.pacing import parse_retry_after
from dexpace.sdk.core.pipeline.policies import RetryPolicy

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


def _get() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


def _post() -> Request:
    return Request(method=Method.POST, url=Url.parse("https://example.com/"))


class _ScriptedClient(HttpClient):
    """Returns one response or raises one error per call, in order."""

    def __init__(
        self,
        outcomes: Sequence[Status | BaseException],
        retry_after: str | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self.retry_after = retry_after
        self.attempts = 0

    def execute(self, request: Request) -> Response:
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        response = Response(request=request, protocol=Protocol.HTTP_1_1, status=outcome)
        if self.retry_after is not None and not outcome.is_success:
            response = response.with_header("Retry-After", self.retry_after)
        return response


class TestRetryOnStatus:
    @pytest.mark.req("RECOV-19")
    def test_retries_503_on_get(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "1")))
        assert response.status is Status.OK
        assert client.attempts == 2

    @pytest.mark.req("RECOV-19")
    def test_does_not_retry_404(self) -> None:
        client = _ScriptedClient([Status.NOT_FOUND])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "2")))
        assert response.status is Status.NOT_FOUND
        assert client.attempts == 1

    @pytest.mark.req("BODY-5")
    def test_post_retried_only_on_500_503_504(self) -> None:
        client = _ScriptedClient([Status.BAD_REQUEST])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_post(), DispatchContext(_instr("0" * 16 + "3")))
        assert client.attempts == 1
        assert response.status is Status.BAD_REQUEST

    def test_post_retried_on_503(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_post(), DispatchContext(_instr("0" * 16 + "4")))
        assert client.attempts == 2
        assert response.is_success

    def test_status_retry_budget_exhausted(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE] * 5)
        retry = RetryPolicy(status_retries=2, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "5")))
        # 1 initial + 2 retries = 3 attempts before giving up.
        assert client.attempts == 3
        assert response.status is Status.SERVICE_UNAVAILABLE


class TestCustomRetryStatusSet:
    """The configurable ``retry_on_status_codes`` set drives the retry decision.

    The default set (``408/429/500/502/503/504``) is the single authority
    shared with the classifier and ``HttpResponseError.retryable``; a caller
    may override it end to end. These tests exercise the actual retry loop, not
    just the pure classifier, so a regression that ignored the configured set
    would be caught here.
    """

    @pytest.mark.req("XCUT-7")
    def test_default_set_is_exactly_the_curated_codes(self) -> None:
        # The policy default is the shared classifier default verbatim — the
        # permanent 501/505 are deliberately absent so retrying a request that
        # can never succeed is not attempted.
        retry = RetryPolicy(clock=FakeClock())
        assert retry.retry_on_status_codes == DEFAULT_RETRYABLE_STATUS
        assert retry.retry_on_status_codes == frozenset({408, 429, 500, 502, 503, 504})
        assert 501 not in retry.retry_on_status_codes
        assert 505 not in retry.retry_on_status_codes

    @pytest.mark.parametrize(
        "status",
        [Status.NOT_IMPLEMENTED, Status.HTTP_VERSION_NOT_SUPPORTED],
        ids=["501", "505"],
    )
    def test_default_policy_does_not_retry_permanent_5xx(self, status: Status) -> None:
        # The permanent-failure exclusion survives end to end: an idempotent GET
        # returning 501/505 is passed straight back, never retried.
        client = _ScriptedClient([status])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 15 + "50")))
        assert client.attempts == 1
        assert response.status is status

    @pytest.mark.req("RETRY-37", "XCUT-7")
    def test_custom_set_honors_explicitly_included_501(self) -> None:
        # A caller who *explicitly* opts 501 into their own set is honored — the
        # default merely excludes it, it is not hard-blocked. The GET is retried
        # and eventually succeeds.
        client = _ScriptedClient([Status.NOT_IMPLEMENTED, Status.OK])
        retry = RetryPolicy(retry_on_status_codes={501}, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 15 + "51")))
        assert client.attempts == 2
        assert response.status is Status.OK

    @pytest.mark.req("RECOV-17", "RETRY-37", "XCUT-7")
    def test_custom_set_excludes_a_default_code(self) -> None:
        # Narrowing the set to ``{500}`` drops 503 from the retryable codes, so a
        # 503 that the default set *would* retry is now returned on first sight.
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE])
        retry = RetryPolicy(retry_on_status_codes={500}, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 15 + "52")))
        assert client.attempts == 1
        assert response.status is Status.SERVICE_UNAVAILABLE


class TestRetryOnError:
    @pytest.mark.req("RECOV-17", "RETRY-37", "RETRY-39", "RETRY-4")
    def test_retries_connect_error(self) -> None:
        client = _ScriptedClient(
            [ServiceRequestError("dns fail"), Status.OK],
        )
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "6")))
        assert response.is_success
        assert client.attempts == 2

    @pytest.mark.req("RETRY-4")
    def test_retries_response_error(self) -> None:
        client = _ScriptedClient(
            [ServiceResponseError("connection reset"), Status.OK],
        )
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "7")))
        assert response.is_success
        assert client.attempts == 2

    def test_short_circuits_client_authentication_error(self) -> None:
        from dexpace.sdk.core.errors import HttpResponseError

        client = _ScriptedClient(
            [ClientAuthenticationError(response=None), Status.OK],
        )
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(HttpResponseError):
            p.run(_get(), DispatchContext(_instr("0" * 16 + "8")))
        # Only one attempt — auth failures are not retried.
        assert client.attempts == 1

    @pytest.mark.req("RECOV-29")
    def test_connect_retry_budget_exhausted(self) -> None:
        client = _ScriptedClient(
            [ServiceRequestError("fail")] * 5,
        )
        retry = RetryPolicy(connect_retries=2, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(ServiceRequestError):
            p.run(_get(), DispatchContext(_instr("0" * 16 + "9")))
        assert client.attempts == 3


class TestReadPhaseMethodGating:
    """Retry-safety is decided uniformly for every transport failure.

    The method-idempotency and body-replayability axes gate every retry
    candidate, connect-phase and read-phase alike. A body-less non-idempotent
    method (a bare POST/PATCH) is therefore never retried on *any* transport
    error — not even a connect-phase ``ServiceRequestError`` that never left
    the client — because retry-safety is a property of the request, decided
    independently of which transport phase failed (XCUT-10). An idempotent GET
    stays retryable on both phases.
    """

    @pytest.mark.req("BODY-5", "RETRY-7")
    def test_post_not_retried_on_read_phase_error(self) -> None:
        # A ``ServiceResponseError`` on a POST must propagate after the first
        # attempt rather than re-sending and risking a duplicate write.
        client = _ScriptedClient([ServiceResponseError("connection reset"), Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(ServiceResponseError):
            p.run(_post(), DispatchContext(_instr("0" * 15 + "40")))
        assert client.attempts == 1

    @pytest.mark.req("XCUT-10")
    def test_post_not_retried_on_connect_phase_error(self) -> None:
        # XCUT-10: the safety gate MUST NOT special-case transport errors. A
        # bare POST is not in the idempotent-method set, so even a connect-phase
        # ``ServiceRequestError`` — which never reached the server — must NOT be
        # retried; it propagates after the first attempt.
        client = _ScriptedClient([ServiceRequestError("dns fail"), Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(ServiceRequestError):
            p.run(_post(), DispatchContext(_instr("0" * 15 + "41")))
        assert client.attempts == 1

    @pytest.mark.req("XCUT-10")
    def test_get_still_retried_on_connect_phase_error(self) -> None:
        # The uniform gate does not suppress idempotent methods: a GET is in the
        # allowlist, so a connect-phase error is still retried to success.
        client = _ScriptedClient([ServiceRequestError("dns fail"), Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 15 + "43")))
        assert response.is_success
        assert client.attempts == 2

    @pytest.mark.req("BODY-5")
    def test_get_still_retried_on_read_phase_error(self) -> None:
        # GET is idempotent and in the allowlist, so a read-phase error is
        # still retried — the gating only suppresses non-idempotent methods.
        client = _ScriptedClient([ServiceResponseError("connection reset"), Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 15 + "42")))
        assert response.is_success
        assert client.attempts == 2


class TestRetryAfterHeader:
    def test_parse_delta_seconds(self) -> None:
        assert parse_retry_after("5", now=0.0) == pytest.approx(5.0)
        assert parse_retry_after("0", now=0.0) == pytest.approx(0.0)
        assert parse_retry_after("0.5", now=0.0) == pytest.approx(0.5)

    def test_parse_http_date_future(self) -> None:
        # Far-future date should be a positive delta.
        result = parse_retry_after("Sun, 06 Nov 2099 08:49:37 GMT", now=0.0)
        assert result is not None
        assert result > 0

    def test_parse_http_date_past_clamps_to_zero(self) -> None:
        # ``now`` is well past the 1990 header instant, so the delta is
        # negative and clamps to zero.
        result = parse_retry_after("Mon, 01 Jan 1990 00:00:00 GMT", now=2_000_000_000.0)
        assert result == pytest.approx(0.0)

    def test_parse_http_date_uses_injected_now(self) -> None:
        # The HTTP-date branch computes ``when.timestamp() - now`` from the
        # injected clock, not the wall clock. 2000-01-01T00:00:00Z is epoch
        # 946684800; with ``now`` set 60s before it, the delay is exactly 60s.
        epoch_2000 = 946_684_800.0
        result = parse_retry_after(
            "Sat, 01 Jan 2000 00:00:00 GMT",
            now=epoch_2000 - 60.0,
        )
        assert result == pytest.approx(60.0)

    def test_parse_invalid_returns_none(self) -> None:
        assert parse_retry_after("not-a-date", now=0.0) is None
        assert parse_retry_after("", now=0.0) is None
        assert parse_retry_after(None, now=0.0) is None

    @pytest.mark.req("RECOV-22", "RETRY-20", "RETRY-39")
    def test_respected_during_retry(self) -> None:
        sleeps: list[float] = []

        class _RecordingClock(FakeClock):
            def sleep(self, duration: float) -> None:
                sleeps.append(duration)
                super().sleep(duration)

        client = _ScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.OK],
            retry_after="2",
        )
        retry = RetryPolicy(clock=_RecordingClock())
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "a")))
        assert sleeps and sleeps[0] == pytest.approx(2.0)

    def test_http_date_uses_injected_clock_not_wall_clock(self) -> None:
        # An HTTP-date ``Retry-After`` must be measured against the injected
        # clock's wall-time. The FakeClock starts 30s before the header's
        # instant, so the policy sleeps exactly 30s — deterministic regardless
        # of the real wall clock.
        epoch_2000 = 946_684_800.0
        clock = FakeClock(start=epoch_2000 - 30.0)
        client = _ScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.OK],
            retry_after="Sat, 01 Jan 2000 00:00:00 GMT",
        )
        retry = RetryPolicy(clock=clock)
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "f1")))
        assert response.status is Status.OK
        # Wall clock advanced by the slept 30s (FakeClock.sleep moves both).
        # Absolute tolerance: a relative approx at this magnitude (~946s band)
        # would mask a clock that never advanced.
        assert clock.monotonic() == pytest.approx(epoch_2000, abs=1.0)


class TestRetryHistory:
    @pytest.mark.req("RECOV-28")
    def test_history_recorded_in_ctx_data(self) -> None:
        from dexpace.sdk.core.pipeline import PipelineContext, Policy

        captured: dict[str, object] = {}

        class _Probe(Policy):
            def send(self, request: Request, ctx: PipelineContext) -> Response:
                response = self.next.send(request, ctx)
                captured["history"] = ctx.data.get("retry_history")
                return response

        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        with Pipeline(client, policies=[_Probe(), RetryPolicy(clock=FakeClock())]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "b")))
        history = captured["history"]
        assert history is not None and len(history) == 1  # type: ignore[arg-type]


class TestRetryNoRetries:
    def test_no_retries_factory(self) -> None:
        retry = RetryPolicy.no_retries()
        assert retry.total_retries == 0

    @pytest.mark.req("RETRY-41")
    def test_no_retries_lets_first_failure_through(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE])
        retry = RetryPolicy.no_retries()
        retry._clock = FakeClock()
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "c")))
        assert client.attempts == 1
        assert response.status is Status.SERVICE_UNAVAILABLE


class TestRetryTimeout:
    @pytest.mark.req("RETRY-41")
    def test_per_call_override_via_options(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE] * 5)
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(
                _get(),
                DispatchContext(_instr("0" * 16 + "d")),
                retry_status=1,
            )
        # 1 initial + 1 retry = 2 attempts.
        assert client.attempts == 2
        assert response.status is Status.SERVICE_UNAVAILABLE


class _BodyRecordingClient(HttpClient):
    """Scripted client that records the body bytes consumed on each attempt."""

    def __init__(
        self,
        outcomes: Sequence[Status | BaseException],
        consumed: list[bytes],
    ) -> None:
        self._outcomes = list(outcomes)
        self._consumed = consumed
        self.attempts = 0

    def execute(self, request: Request) -> Response:
        body = request.body
        captured = b"".join(body.iter_bytes()) if body is not None else b""
        self._consumed.append(captured)
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=outcome)


class TestRetryAutoReplaysBody:
    @pytest.mark.req("BODY-4", "RETRY-44")
    def test_retry_with_single_use_body_auto_replays(self) -> None:
        consumed: list[bytes] = []
        body = RequestBody.from_iter(iter([b"hello", b"world"]))
        request = Request(method=Method.POST, url=Url.parse("https://example.com/"), body=body)
        client = _BodyRecordingClient(
            [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK],
            consumed,
        )
        retry = RetryPolicy(total_retries=2, backoff_factor=0, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(request, DispatchContext(_instr("0" * 16 + "e")))
        assert response.is_success
        assert consumed == [b"helloworld", b"helloworld", b"helloworld"]

    def test_retry_no_retries_leaves_body_alone(self) -> None:
        observed: list[RequestBody | None] = []

        class _CapturingClient(HttpClient):
            def execute(self, request: Request) -> Response:
                observed.append(request.body)
                return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)

        body = RequestBody.from_iter(iter([b"hello", b"world"]))
        request = Request(method=Method.POST, url=Url.parse("https://example.com/"), body=body)
        retry = RetryPolicy.no_retries()
        retry._clock = FakeClock()
        with Pipeline(_CapturingClient(), policies=[retry]) as p:
            response = p.run(request, DispatchContext(_instr("0" * 16 + "f")))
        assert response.is_success
        # ``total_retries=0`` skips the auto-replay buffering — the body the
        # transport receives is the original single-use instance.
        assert observed == [body]
        assert not body.is_replayable()

    def test_per_call_retry_total_over_zero_instance_buffers_body(self) -> None:
        # The exact H2 failure scenario: an instance built with
        # ``total_retries=0`` plus a per-call ``retry_total=3`` and a
        # single-use ``from_iter`` body. Buffering keyed off the constructor
        # default would skip ``to_replayable`` and the second ``iter_bytes``
        # would raise ``RuntimeError`` mid-retry. Keying off the effective
        # per-call total buffers the body, so the retry re-emits the payload.
        consumed: list[bytes] = []
        body = RequestBody.from_iter(iter([b"hello", b"world"]))
        request = Request(method=Method.POST, url=Url.parse("https://example.com/"), body=body)
        client = _BodyRecordingClient(
            [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK],
            consumed,
        )
        retry = RetryPolicy(total_retries=0, backoff_factor=0, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(
                request,
                DispatchContext(_instr("0" * 15 + "30")),
                retry_total=3,
            )
        assert response.is_success
        # Every attempt re-emitted the full payload — no ``RuntimeError`` from a
        # second consumption of the single-use iterator.
        assert consumed == [b"helloworld", b"helloworld", b"helloworld"]

    def test_per_call_retry_total_zero_over_retrying_instance_skips_buffering(self) -> None:
        # Inverse of the H2 scenario: a retrying instance with a per-call
        # ``retry_total=0`` must not pay the memory cost of buffering a
        # single-use body it will never replay.
        observed: list[RequestBody | None] = []

        class _CapturingClient(HttpClient):
            def execute(self, request: Request) -> Response:
                observed.append(request.body)
                return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)

        body = RequestBody.from_iter(iter([b"hello", b"world"]))
        request = Request(method=Method.POST, url=Url.parse("https://example.com/"), body=body)
        retry = RetryPolicy(total_retries=5, clock=FakeClock())
        with Pipeline(_CapturingClient(), policies=[retry]) as p:
            response = p.run(
                request,
                DispatchContext(_instr("0" * 15 + "31")),
                retry_total=0,
            )
        assert response.is_success
        # The per-call override drives the buffering decision: ``retry_total=0``
        # leaves the original single-use body untouched.
        assert observed == [body]
        assert not body.is_replayable()


class TestRetryJitter:
    def test_jitter_varies_backoff(self) -> None:
        retry = RetryPolicy(
            backoff_factor=1.0,
            backoff_max=1000.0,
            full_jitter=False,
            jitter=0.25,
            rand=random.Random(42),
            clock=FakeClock(),
        )
        # ``_backoff_seconds`` keys off the number of attempts in history; fake
        # a long-running settings dict so the same exponent is sampled twice.
        settings: dict[str, Any] = {
            "backoff": 1.0,
            "max_backoff": 1000.0,
            "history": [None, None, None],
        }
        first = retry._backoff_seconds(settings)
        second = retry._backoff_seconds(settings)
        assert first != second
        # Both samples land inside the ±25% band around the deterministic base.
        base = float(settings["backoff"]) * (2 ** (len(settings["history"]) - 1))
        assert 0.75 * base <= first <= 1.25 * base
        assert 0.75 * base <= second <= 1.25 * base

    def test_no_jitter_when_zero(self) -> None:
        retry = RetryPolicy(
            backoff_factor=1.0,
            backoff_max=1000.0,
            full_jitter=False,
            jitter=0.0,
            rand=random.Random(42),
            clock=FakeClock(),
        )
        settings: dict[str, Any] = {
            "backoff": 1.0,
            "max_backoff": 1000.0,
            "history": [None, None, None],
        }
        first = retry._backoff_seconds(settings)
        second = retry._backoff_seconds(settings)
        base = float(settings["backoff"]) * (2 ** (len(settings["history"]) - 1))
        assert first == pytest.approx(base)
        assert second == pytest.approx(base)


class TestRetryCountTiming:
    def test_retry_count_not_set_when_no_retry_happens(self) -> None:
        from dexpace.sdk.core.pipeline import PipelineContext, Policy

        captured: dict[str, object] = {}

        class _Probe(Policy):
            def send(self, request: Request, ctx: PipelineContext) -> Response:
                response = self.next.send(request, ctx)
                captured["retry_count"] = ctx.data.get("retry_count")
                return response

        client = _ScriptedClient([Status.OK])
        with Pipeline(client, policies=[_Probe(), RetryPolicy(clock=FakeClock())]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 15 + "10")))
        # When the request succeeds first time, no retry decision is made, so
        # ``retry_count`` is never written into ``ctx.data``.
        assert captured["retry_count"] is None

    @pytest.mark.req("RECOV-28")
    def test_retry_count_set_when_retry_happens(self) -> None:
        from dexpace.sdk.core.pipeline import PipelineContext, Policy

        captured: dict[str, object] = {}

        class _Probe(Policy):
            def send(self, request: Request, ctx: PipelineContext) -> Response:
                response = self.next.send(request, ctx)
                captured["retry_count"] = ctx.data.get("retry_count")
                return response

        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        with Pipeline(client, policies=[_Probe(), RetryPolicy(clock=FakeClock())]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 15 + "11")))
        # One retry occurred — the count reflects the single failed attempt.
        assert captured["retry_count"] == 1


class TestRequestHistoryTyping:
    """``RequestHistory`` is parametrised by response type.

    The real assertion is that ``mypy --strict`` accepts these annotations
    — i.e. ``RequestHistory[Response].response`` is ``Response | None``,
    not ``Response | AsyncResponse | None``. The runtime check below is a
    smoke test that construction works.
    """

    def test_request_history_response_field_typed_as_response(self) -> None:
        from dexpace.sdk.core.pipeline.policies._history import RequestHistory

        request = _get()
        response = Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.OK,
        )
        entry: RequestHistory[Response] = RequestHistory(
            request=request,
            response=response,
        )
        # mypy narrows ``entry.response`` to ``Response | None`` — no
        # ``AsyncResponse`` branch to disambiguate at the call site.
        assert entry.response is response
        assert entry.error is None

    def test_request_history_error_branch_has_none_response(self) -> None:
        from dexpace.sdk.core.pipeline.policies._history import RequestHistory

        request = _get()
        entry: RequestHistory[Response] = RequestHistory(
            request=request,
            error=ServiceRequestError("boom"),
        )
        assert entry.response is None
        assert isinstance(entry.error, ServiceRequestError)


# Sanity: time.monotonic available for the budget arithmetic used internally.
def test_monotonic_clock_available() -> None:
    assert time.monotonic() > 0


@pytest.mark.req("RETRY-36")
def test_retry_advances_clock_by_backoff_seconds() -> None:
    """``RetryPolicy`` advances the injected clock by the computed backoff.

    The fake clock starts at ``t=0``; each ``sleep`` call accrues into the
    monotonic reading. With ``jitter=0`` and ``EXPONENTIAL`` mode the first
    retry sleep is zero (attempts == 1) and the second is
    ``backoff_factor * 2 ** (attempts - 1) = 1.0 * 2 = 2.0`` seconds. The
    fake clock's reading after the run reflects the cumulative sleep.
    """
    clock = FakeClock()
    client = _ScriptedClient(
        [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK],
    )
    retry = RetryPolicy(
        backoff_factor=1.0,
        backoff_max=1000.0,
        full_jitter=False,
        jitter=0.0,
        clock=clock,
    )
    with Pipeline(client, policies=[retry]) as p:
        response = p.run(_get(), DispatchContext(_instr("0" * 15 + "20")))
    assert response.is_success
    assert client.attempts == 3
    # First retry: 0s (attempts==1 returns 0). Second retry: 2.0s.
    assert clock.monotonic() == pytest.approx(2.0)


def test_retry_clock_jitter_seeded() -> None:
    """Backoff durations are reproducible across runs with a seeded RNG.

    Constructing two policies with identical seeds and driving the same
    settings dict through ``_backoff_seconds`` yields the same sequence —
    the jitter is a pure function of the seeded ``random.Random``.
    """
    settings: dict[str, Any] = {
        "backoff": 1.0,
        "max_backoff": 1000.0,
        "history": [None, None, None],
    }
    retry_a = RetryPolicy(
        backoff_factor=1.0,
        backoff_max=1000.0,
        jitter=0.25,
        rand=random.Random(42),
        clock=FakeClock(),
    )
    retry_b = RetryPolicy(
        backoff_factor=1.0,
        backoff_max=1000.0,
        jitter=0.25,
        rand=random.Random(42),
        clock=FakeClock(),
    )
    sequence_a = [retry_a._backoff_seconds(dict(settings)) for _ in range(5)]
    sequence_b = [retry_b._backoff_seconds(dict(settings)) for _ in range(5)]
    assert sequence_a == sequence_b
    # And the values are non-trivial (jitter actually varied them).
    assert len(set(sequence_a)) > 1


class _CustomRetryableError(SdkError):
    """A non-protocol custom SDK error advertising retryability via capability.

    It is neither a ``ServiceRequestError``/``ServiceResponseError`` nor a
    status-carrying ``HttpResponseError`` — only the structural ``retryable``
    flag marks it retryable, exercising the open-capability path.
    """

    retryable = True


class _CustomTerminalError(SdkError):
    """A custom SDK error that declares itself non-retryable via the capability."""

    retryable = False


class TestOpenCapabilityRetry:
    """Custom/transport-family errors participate via the retryability capability.

    The retry step queries the capability (``is_retryable`` over the cause
    chain), not a concrete-type match, so an error type the classifier has
    never heard of can opt into retries (XCUT-6). A status-carrying protocol
    error is handled by the configured status set instead, never by this path.
    """

    @pytest.mark.req("XCUT-6")
    def test_custom_error_advertising_retryable_is_retried(self) -> None:
        client = _ScriptedClient([_CustomRetryableError("boom"), Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 15 + "80")))
        assert response.is_success
        assert client.attempts == 2

    @pytest.mark.req("XCUT-6")
    def test_custom_error_not_advertising_retryable_is_terminal(self) -> None:
        client = _ScriptedClient([_CustomTerminalError("nope"), Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(_CustomTerminalError):
            p.run(_get(), DispatchContext(_instr("0" * 15 + "81")))
        assert client.attempts == 1

    @pytest.mark.req("XCUT-6")
    def test_capability_retry_is_bounded_by_the_connect_budget(self) -> None:
        # The capability path is charged to the connect sub-budget, so a
        # never-clearing custom retryable error stops after connect_retries.
        client = _ScriptedClient([_CustomRetryableError("boom")] * 6)
        retry = RetryPolicy(connect_retries=2, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(_CustomRetryableError):
            p.run(_get(), DispatchContext(_instr("0" * 15 + "82")))
        # 1 initial + 2 connect retries = 3 attempts before the budget is spent.
        assert client.attempts == 3

    @pytest.mark.req("XCUT-6", "XCUT-10")
    def test_capability_retry_still_honours_the_uniform_safety_gate(self) -> None:
        # A capability-retryable error on a bare POST is NOT retried: the
        # method-idempotency safety gate applies uniformly to this path too.
        client = _ScriptedClient([_CustomRetryableError("boom"), Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(_CustomRetryableError):
            p.run(_post(), DispatchContext(_instr("0" * 15 + "83")))
        assert client.attempts == 1


class TestRetryConfigValidation:
    """Scalar configuration is validated and rejected at construction (RECOV-34)."""

    @pytest.mark.req("RECOV-34")
    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"total_retries": -1}, "total_retries"),
            ({"connect_retries": -1}, "connect_retries"),
            ({"read_retries": -1}, "read_retries"),
            ({"status_retries": -1}, "status_retries"),
            ({"backoff_factor": -0.1}, "backoff_factor"),
            ({"backoff_max": -1.0}, "backoff_max"),
            ({"timeout": -1.0}, "timeout"),
            ({"retry_after_max": -1.0}, "retry_after_max"),
            ({"backoff_factor": float("nan")}, "backoff_factor"),
            ({"backoff_max": float("inf")}, "backoff_max"),
            ({"jitter": 1.5}, "jitter"),
            ({"jitter": -0.1}, "jitter"),
        ],
    )
    def test_rejects_invalid_scalar(self, kwargs: dict[str, Any], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            RetryPolicy(**kwargs)

    @pytest.mark.req("RECOV-34")
    def test_boundary_values_are_accepted(self) -> None:
        # Zero counts/durations disable an axis but are valid; jitter is valid at
        # both ends of the closed interval [0.0, 1.0].
        RetryPolicy(
            total_retries=0,
            connect_retries=0,
            read_retries=0,
            status_retries=0,
            backoff_factor=0.0,
            backoff_max=0.0,
            timeout=0.0,
            retry_after_max=0.0,
            jitter=0.0,
        )
        RetryPolicy(jitter=1.0)

    @pytest.mark.req("RECOV-34")
    def test_collection_settings_are_defensively_copied(self) -> None:
        # Later mutation of the caller's source collections cannot alter the
        # already-constructed policy's behaviour.
        statuses = {500, 503}
        methods = {"GET", "PUT"}
        retry = RetryPolicy(retry_on_status_codes=statuses, method_allowlist=methods)
        statuses.add(418)
        methods.add("POST")
        assert 418 not in retry.retry_on_status_codes
        assert "POST" not in retry.method_allowlist
        assert retry.retry_on_status_codes == frozenset({500, 503})


class TestFatalErrorPropagation:
    """Non-SDK fatal errors surface unchanged, unretried, and untrailed (RETRY-25)."""

    @pytest.mark.req("RETRY-25")
    @pytest.mark.parametrize(
        "fatal",
        [MemoryError("out of memory"), RecursionError("stack overflow")],
        ids=["MemoryError", "RecursionError"],
    )
    def test_non_sdk_fatal_propagates_unretried_untrailed(
        self, fatal: BaseException, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _ScriptedClient([fatal, Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with (
            caplog.at_level(logging.DEBUG),
            Pipeline(client, policies=[retry]) as p,
            pytest.raises(type(fatal)) as excinfo,
        ):
            p.run(_get(), DispatchContext(_instr("0" * 15 + "90")))
        # Surfaced unchanged (exact instance), never retried, and never
        # classified as a retryable condition.
        assert excinfo.value is fatal
        assert client.attempts == 1
        # No suppressed-trail attachment: neither the ``attempts`` count nor the
        # per-attempt ``add_note`` trail is stamped onto a fatal error.
        assert not hasattr(fatal, "attempts")
        assert getattr(fatal, "__notes__", []) == []
        # Not logged by the retry loop (the "retrying after ..." debug line only
        # fires on the retry path, which a fatal error never takes).
        assert not any(
            record.name == "dexpace.sdk.core.pipeline.policies.retry" for record in caplog.records
        )


class _ConcurrentClient(HttpClient):
    """Thread-safe client: fails a request ``fail_times`` then succeeds.

    Keyed by request URL so each concurrent caller (distinct URL) has an
    independent attempt tally — the shared counter is guarded by a lock.
    """

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()
        self.total_attempts = 0

    def execute(self, request: Request) -> Response:
        key = str(request.url)
        with self._lock:
            seen = self._seen.get(key, 0)
            self._seen[key] = seen + 1
            self.total_attempts += 1
        status = Status.OK if seen >= self._fail_times else Status.SERVICE_UNAVAILABLE
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=status)


class TestRetryStatelessness:
    """One policy instance is stateless and safe to invoke concurrently (RETRY-42)."""

    @pytest.mark.req("RETRY-42")
    def test_single_instance_safe_under_concurrent_calls(self) -> None:
        # backoff_factor=0 keeps every delay zero, so no thread mutates the
        # shared clock — the run isolates policy statelessness, not clock races.
        retry = RetryPolicy(backoff_factor=0.0, clock=FakeClock())
        client = _ConcurrentClient(fail_times=1)
        results: dict[int, Status] = {}
        failures: list[BaseException] = []
        lock = threading.Lock()

        with Pipeline(client, policies=[retry]) as pipeline:

            def worker(index: int) -> None:
                try:
                    request = Request(
                        method=Method.GET, url=Url.parse(f"https://example.com/{index}")
                    )
                    response = pipeline.run(request, DispatchContext(_instr(f"{index:032x}")))
                    with lock:
                        results[index] = response.status
                except BaseException as error:  # surfaced to the assertion
                    with lock:
                        failures.append(error)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert not failures
        assert len(results) == 24
        assert all(status is Status.OK for status in results.values())
        # Each call: one 503 then one 200 = two transport attempts, independently.
        assert client.total_attempts == 48
        # The shared instance's frozen knobs are untouched by the concurrent run.
        assert retry.total_retries == 10
        assert retry.connect_retries == 3

    @pytest.mark.req("RETRY-42")
    def test_repeated_calls_on_one_instance_are_independent(self) -> None:
        # Repeatedly invoking one policy instance (one pipeline) carries no
        # per-call state — attempt count, deadline, and history — between
        # sequential calls: each distinct URL fails once then succeeds cleanly.
        retry = RetryPolicy(backoff_factor=0.0, clock=FakeClock())
        client = _ConcurrentClient(fail_times=1)
        with Pipeline(client, policies=[retry]) as pipeline:
            for index in range(3):
                request = Request(
                    method=Method.GET, url=Url.parse(f"https://example.com/seq/{index}")
                )
                response = pipeline.run(request, DispatchContext(_instr(f"{index:032x}")))
                assert response.is_success
        # Three calls, each one 503 + one 200 = six independent attempts.
        assert client.total_attempts == 6
        assert retry.total_retries == 10
