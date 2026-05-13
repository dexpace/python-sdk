"""Tests for ``RetryPolicy`` behaviour."""

from __future__ import annotations

import random
import time
from collections.abc import Sequence
from typing import Any

import pytest

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import (
    ClientAuthenticationError,
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
from dexpace.sdk.core.pipeline.policies import RetryPolicy
from dexpace.sdk.core.pipeline.policies.retry import _parse_retry_after


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


def _no_sleep(_duration: float) -> None:
    return None


class TestRetryOnStatus:
    def test_retries_503_on_get(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = RetryPolicy(sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "1")))
        assert response.status is Status.OK
        assert client.attempts == 2

    def test_does_not_retry_404(self) -> None:
        client = _ScriptedClient([Status.NOT_FOUND])
        retry = RetryPolicy(sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "2")))
        assert response.status is Status.NOT_FOUND
        assert client.attempts == 1

    def test_post_retried_only_on_500_503_504(self) -> None:
        client = _ScriptedClient([Status.BAD_REQUEST])
        retry = RetryPolicy(sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_post(), DispatchContext(_instr("0" * 16 + "3")))
        assert client.attempts == 1
        assert response.status is Status.BAD_REQUEST

    def test_post_retried_on_503(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = RetryPolicy(sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_post(), DispatchContext(_instr("0" * 16 + "4")))
        assert client.attempts == 2
        assert response.is_success

    def test_status_retry_budget_exhausted(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE] * 5)
        retry = RetryPolicy(status_retries=2, sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "5")))
        # 1 initial + 2 retries = 3 attempts before giving up.
        assert client.attempts == 3
        assert response.status is Status.SERVICE_UNAVAILABLE


class TestRetryOnError:
    def test_retries_connect_error(self) -> None:
        client = _ScriptedClient(
            [ServiceRequestError("dns fail"), Status.OK],
        )
        retry = RetryPolicy(sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "6")))
        assert response.is_success
        assert client.attempts == 2

    def test_retries_response_error(self) -> None:
        client = _ScriptedClient(
            [ServiceResponseError("connection reset"), Status.OK],
        )
        retry = RetryPolicy(sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "7")))
        assert response.is_success
        assert client.attempts == 2

    def test_short_circuits_client_authentication_error(self) -> None:
        from dexpace.sdk.core.errors import HttpResponseError

        client = _ScriptedClient(
            [ClientAuthenticationError(response=None), Status.OK],
        )
        retry = RetryPolicy(sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p, pytest.raises(HttpResponseError):
            p.run(_get(), DispatchContext(_instr("0" * 16 + "8")))
        # Only one attempt — auth failures are not retried.
        assert client.attempts == 1

    def test_connect_retry_budget_exhausted(self) -> None:
        client = _ScriptedClient(
            [ServiceRequestError("fail")] * 5,
        )
        retry = RetryPolicy(connect_retries=2, sleep=_no_sleep)
        with Pipeline(client, policies=[retry]) as p, pytest.raises(ServiceRequestError):
            p.run(_get(), DispatchContext(_instr("0" * 16 + "9")))
        assert client.attempts == 3


class TestRetryAfterHeader:
    def test_parse_delta_seconds(self) -> None:
        assert _parse_retry_after("5") == pytest.approx(5.0)
        assert _parse_retry_after("0") == pytest.approx(0.0)
        assert _parse_retry_after("0.5") == pytest.approx(0.5)

    def test_parse_http_date_future(self) -> None:
        # Far-future date should be a positive delta.
        result = _parse_retry_after("Sun, 06 Nov 2099 08:49:37 GMT")
        assert result is not None
        assert result > 0

    def test_parse_http_date_past_clamps_to_zero(self) -> None:
        result = _parse_retry_after("Mon, 01 Jan 1990 00:00:00 GMT")
        assert result == pytest.approx(0.0)

    def test_parse_invalid_returns_none(self) -> None:
        assert _parse_retry_after("not-a-date") is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after(None) is None

    def test_respected_during_retry(self) -> None:
        sleeps: list[float] = []

        def record(d: float) -> None:
            sleeps.append(d)

        client = _ScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.OK],
            retry_after="2",
        )
        retry = RetryPolicy(sleep=record)
        with Pipeline(client, policies=[retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "a")))
        assert sleeps and sleeps[0] == pytest.approx(2.0)


class TestRetryHistory:
    def test_history_recorded_in_ctx_data(self) -> None:
        from dexpace.sdk.core.pipeline import PipelineContext, Policy

        captured: dict[str, object] = {}

        class _Probe(Policy):
            def send(self, request: Request, ctx: PipelineContext) -> Response:
                response = self.next.send(request, ctx)
                captured["history"] = ctx.data.get("retry_history")
                return response

        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        with Pipeline(client, policies=[_Probe(), RetryPolicy(sleep=_no_sleep)]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 16 + "b")))
        history = captured["history"]
        assert history is not None and len(history) == 1  # type: ignore[arg-type]


class TestRetryNoRetries:
    def test_no_retries_factory(self) -> None:
        retry = RetryPolicy.no_retries()
        assert retry.total_retries == 0

    def test_no_retries_lets_first_failure_through(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE])
        retry = RetryPolicy.no_retries()
        retry._sleep = _no_sleep
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "c")))
        assert client.attempts == 1
        assert response.status is Status.SERVICE_UNAVAILABLE


class TestRetryTimeout:
    def test_per_call_override_via_options(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE] * 5)
        retry = RetryPolicy(sleep=_no_sleep)
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
    def test_retry_with_single_use_body_auto_replays(self) -> None:
        consumed: list[bytes] = []
        body = RequestBody.from_iter(iter([b"hello", b"world"]))
        request = Request(method=Method.POST, url=Url.parse("https://example.com/"), body=body)
        client = _BodyRecordingClient(
            [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK],
            consumed,
        )
        retry = RetryPolicy(total_retries=2, backoff_factor=0, sleep=_no_sleep)
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
        retry._sleep = _no_sleep
        with Pipeline(_CapturingClient(), policies=[retry]) as p:
            response = p.run(request, DispatchContext(_instr("0" * 16 + "f")))
        assert response.is_success
        # ``total_retries=0`` skips the auto-replay buffering — the body the
        # transport receives is the original single-use instance.
        assert observed == [body]
        assert not body.is_replayable()


class TestRetryJitter:
    def test_jitter_varies_backoff(self) -> None:
        retry = RetryPolicy(
            backoff_factor=1.0,
            backoff_max=1000.0,
            jitter=0.25,
            rand=random.Random(42),
            sleep=_no_sleep,
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
            jitter=0.0,
            rand=random.Random(42),
            sleep=_no_sleep,
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
        with Pipeline(client, policies=[_Probe(), RetryPolicy(sleep=_no_sleep)]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 15 + "10")))
        # When the request succeeds first time, no retry decision is made, so
        # ``retry_count`` is never written into ``ctx.data``.
        assert captured["retry_count"] is None

    def test_retry_count_set_when_retry_happens(self) -> None:
        from dexpace.sdk.core.pipeline import PipelineContext, Policy

        captured: dict[str, object] = {}

        class _Probe(Policy):
            def send(self, request: Request, ctx: PipelineContext) -> Response:
                response = self.next.send(request, ctx)
                captured["retry_count"] = ctx.data.get("retry_count")
                return response

        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        with Pipeline(client, policies=[_Probe(), RetryPolicy(sleep=_no_sleep)]) as p:
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
