# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Completion contracts for the sync and async retry loops.

Certifies the finished retry mechanics that both stacks must share:

- exact attempt accounting (``max_retries=2`` sends exactly three times);
- prompt cancellation of the backoff wait — cooperative (sync
  ``CancellationToken``) and native (async task cancellation);
- the two-axis re-send safety gate applied uniformly to HTTP-status- and
  transport-error-triggered retries (a bare POST is not retried, a
  non-replayable body is never re-sent regardless of method);
- a terminal-failure attempt trail carried on the surfaced exception (an
  ``attempts`` count plus prior-attempt notes, excluding the exception itself);
- byte-identical backoff schedules across the two stacks for a shared seed.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Iterator, Sequence

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import (
    RequestCancelledError,
    ServiceRequestError,
    ServiceResponseError,
)
from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline
from dexpace.sdk.core.pipeline.policies import RetryPolicy
from dexpace.sdk.core.pipeline.policies.async_retry import AsyncRetryPolicy
from dexpace.sdk.core.util.clock import ASYNC_SYSTEM_CLOCK, CancellationToken

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


def _post(body: RequestBody | None = None) -> Request:
    return Request(method=Method.POST, url=Url.parse("https://example.com/"), body=body)


def _attempts(error: BaseException) -> int:
    """Read the ``attempts`` count the retry loop stamps on a terminal failure.

    The count is set dynamically via ``setattr`` (no static attribute exists on
    the exception types), so the test reads it out of the instance ``__dict__``.
    """
    return int(error.__dict__["attempts"])


class _AsyncFakeClock:
    """Deterministic ``AsyncClock``; advances simulated time on sleep."""

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    async def sleep(self, duration: float) -> None:
        self._t += max(0.0, duration)


class _ScriptedClient(HttpClient):
    """Returns one response or raises one error per call, in order."""

    def __init__(
        self,
        outcomes: Sequence[Status | BaseException],
        retry_after: str | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._retry_after = retry_after
        self.attempts = 0

    def execute(self, request: Request) -> Response:
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        response = Response(request=request, protocol=Protocol.HTTP_1_1, status=outcome)
        if self._retry_after is not None and not outcome.is_success:
            response = response.with_header("Retry-After", self._retry_after)
        return response


class _AsyncScriptedClient(AsyncHttpClient):
    """Async twin of ``_ScriptedClient``."""

    def __init__(
        self,
        outcomes: Sequence[Status | BaseException],
        retry_after: str | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._retry_after = retry_after
        self.attempts = 0

    async def execute(self, request: Request) -> AsyncResponse:
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        response = AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=outcome)
        if self._retry_after is not None and not outcome.is_success:
            response = response.with_header("Retry-After", self._retry_after)
        return response


class _NonReplayableBody(RequestBody):
    """A body that cannot be buffered — ``to_replayable`` refuses to copy it.

    Models a payload the caller declared un-bufferable (an unbounded stream that
    must not be held in memory). ``is_replayable`` stays ``False`` even after
    ``to_replayable``, so the retry loop must never re-send it.
    """

    def __init__(self) -> None:
        self.consumed = 0

    def media_type(self) -> MediaType | None:
        return None

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        self.consumed += 1
        yield b"payload"

    def is_replayable(self) -> bool:
        return False

    def to_replayable(self) -> RequestBody:
        return self


# ----- exact attempt accounting -------------------------------------------


class TestExactlyThreeSends:
    def test_sync_max_retries_two_sends_exactly_three_times(self) -> None:
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE] * 5)
        retry = RetryPolicy(total_retries=2, backoff_factor=0.0, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 15 + "50")))
        # 1 initial + 2 retries = exactly 3 sends; no off-by-one.
        assert client.attempts == 3
        assert response.status is Status.SERVICE_UNAVAILABLE

    async def test_async_max_retries_two_sends_exactly_three_times(self) -> None:
        client = _AsyncScriptedClient([Status.SERVICE_UNAVAILABLE] * 5)
        retry = AsyncRetryPolicy(total_retries=2, backoff_factor=0.0, clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_get(), DispatchContext(_instr("0" * 15 + "51")))
        assert client.attempts == 3
        assert response.status is Status.SERVICE_UNAVAILABLE


# ----- prompt cancellation of the backoff wait ----------------------------


class TestCancelDuringBackoff:
    def test_sync_precancelled_token_aborts_at_attempt_boundary(self) -> None:
        # An already-cancelled token is observed at the attempt boundary before
        # the first send: cooperative cancellation means "do not start".
        token = CancellationToken()
        token.cancel()
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with (
            Pipeline(client, policies=[retry]) as p,
            pytest.raises(RequestCancelledError),
        ):
            p.run(
                _get(),
                DispatchContext(_instr("0" * 15 + "52")),
                cancellation=token,
            )
        assert client.attempts == 0

    def test_sync_cancel_mid_backoff_aborts_promptly(self) -> None:
        token = CancellationToken()
        # Real 30s backoff via a server ``Retry-After``; a timer cancels the
        # token ~50ms in. ``CancellationToken.sleep`` (threading.Event.wait)
        # must wake immediately, so the wait aborts far inside the 30s window.
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK], retry_after="30")
        retry = RetryPolicy(clock=FakeClock())
        timer = threading.Timer(0.05, token.cancel)
        timer.start()
        start = time.monotonic()
        try:
            with (
                Pipeline(client, policies=[retry]) as p,
                pytest.raises(RequestCancelledError),
            ):
                p.run(
                    _get(),
                    DispatchContext(_instr("0" * 15 + "53")),
                    cancellation=token,
                )
        finally:
            timer.cancel()
        elapsed = time.monotonic() - start
        # Generous epsilon: the point is it did not wait the full 30s.
        assert elapsed < 5.0
        assert client.attempts == 1

    async def test_async_cancel_mid_backoff_aborts_immediately(self) -> None:
        # Real ``asyncio.sleep`` backoff of 30s (server ``Retry-After``); the
        # task is cancelled while parked in it and must unwind at once.
        client = _AsyncScriptedClient(
            [Status.SERVICE_UNAVAILABLE, Status.OK],
            retry_after="30",
        )
        retry = AsyncRetryPolicy(clock=ASYNC_SYSTEM_CLOCK)

        async def _drive() -> AsyncResponse:
            async with AsyncPipeline(client, policies=[retry]) as p:
                return await p.run(_get(), DispatchContext(_instr("0" * 15 + "54")))

        task = asyncio.ensure_future(_drive())
        # Let the first send land and the loop enter the 30s backoff.
        await asyncio.sleep(0.1)
        start = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert client.attempts == 1


# ----- two-axis re-send safety gate ---------------------------------------


class TestBarePostNeverRetried:
    def test_sync_bare_post_not_retried_on_retryable_status(self) -> None:
        # 429 is in the default retryable set, but POST is only ever retried on
        # 500/503/504 — a bare POST on 429 is not re-sent.
        client = _ScriptedClient([Status.TOO_MANY_REQUESTS, Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_post(), DispatchContext(_instr("0" * 15 + "55")))
        assert client.attempts == 1
        assert response.status is Status.TOO_MANY_REQUESTS

    async def test_async_bare_post_not_retried_on_retryable_status(self) -> None:
        client = _AsyncScriptedClient([Status.TOO_MANY_REQUESTS, Status.OK])
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_post(), DispatchContext(_instr("0" * 15 + "56")))
        assert client.attempts == 1
        assert response.status is Status.TOO_MANY_REQUESTS


class TestNonReplayableBodyNeverResent:
    def test_sync_status_trigger_does_not_resend_non_replayable_body(self) -> None:
        body = _NonReplayableBody()
        client = _ScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_post(body), DispatchContext(_instr("0" * 15 + "57")))
        # 503 would retry a replayable POST, but the body cannot be replayed.
        assert client.attempts == 1
        assert response.status is Status.SERVICE_UNAVAILABLE

    def test_sync_transport_trigger_does_not_resend_non_replayable_body(self) -> None:
        # PUT is idempotent and a connect-phase error is otherwise always
        # retried — but a non-replayable body overrides method idempotency.
        body = _NonReplayableBody()
        client = _ScriptedClient([ServiceRequestError("dns fail"), Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        request = Request(method=Method.PUT, url=Url.parse("https://example.com/"), body=body)
        with (
            Pipeline(client, policies=[retry]) as p,
            pytest.raises(ServiceRequestError),
        ):
            p.run(request, DispatchContext(_instr("0" * 15 + "58")))
        assert client.attempts == 1

    async def test_async_status_trigger_does_not_resend_non_replayable_body(self) -> None:
        body = _NonReplayableBody()
        client = _AsyncScriptedClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_post(body), DispatchContext(_instr("0" * 15 + "59")))
        assert client.attempts == 1
        assert response.status is Status.SERVICE_UNAVAILABLE

    async def test_async_transport_trigger_does_not_resend_non_replayable_body(self) -> None:
        body = _NonReplayableBody()
        client = _AsyncScriptedClient([ServiceRequestError("dns fail"), Status.OK])
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        request = Request(method=Method.PUT, url=Url.parse("https://example.com/"), body=body)
        with pytest.raises(ServiceRequestError):
            async with AsyncPipeline(client, policies=[retry]) as p:
                await p.run(request, DispatchContext(_instr("0" * 15 + "5a")))
        assert client.attempts == 1


# ----- terminal-failure attempt trail -------------------------------------


class TestTerminalFailureTrail:
    def test_sync_surfaced_error_carries_attempts_and_prior_notes(self) -> None:
        errors: list[BaseException] = [
            ServiceRequestError("boom-1"),
            ServiceRequestError("boom-2"),
            ServiceRequestError("boom-3"),
        ]
        client = _ScriptedClient(errors)
        retry = RetryPolicy(connect_retries=2, backoff_factor=0.0, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(ServiceRequestError) as info:
            p.run(_get(), DispatchContext(_instr("0" * 15 + "5b")))
        err = info.value
        # 1 initial + 2 retries = 3 attempts, all failed.
        assert _attempts(err) == 3
        notes = getattr(err, "__notes__", [])
        # Two PRIOR attempts noted; the surfaced error never notes itself.
        assert len(notes) == 2
        assert any("boom-1" in note for note in notes)
        assert any("boom-2" in note for note in notes)
        assert all("boom-3" not in note for note in notes)

    async def test_async_surfaced_error_carries_attempts_and_prior_notes(self) -> None:
        errors: list[BaseException] = [
            ServiceRequestError("boom-1"),
            ServiceRequestError("boom-2"),
            ServiceRequestError("boom-3"),
        ]
        client = _AsyncScriptedClient(errors)
        retry = AsyncRetryPolicy(connect_retries=2, backoff_factor=0.0, clock=_AsyncFakeClock())
        with pytest.raises(ServiceRequestError) as info:
            async with AsyncPipeline(client, policies=[retry]) as p:
                await p.run(_get(), DispatchContext(_instr("0" * 15 + "5c")))
        err = info.value
        assert _attempts(err) == 3
        notes = getattr(err, "__notes__", [])
        assert len(notes) == 2
        assert all("boom-3" not in note for note in notes)

    def test_status_then_error_trail_excludes_only_the_surfaced_error(self) -> None:
        # A 503 status retry precedes the terminal connect error: the prior
        # status attempt is in the trail, the surfaced error is not.
        outcomes: list[Status | BaseException] = [
            Status.SERVICE_UNAVAILABLE,
            ServiceResponseError("read broke"),
        ]
        client = _ScriptedClient(outcomes)
        retry = RetryPolicy(read_retries=0, backoff_factor=0.0, clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p, pytest.raises(ServiceResponseError) as info:
            p.run(_get(), DispatchContext(_instr("0" * 15 + "5d")))
        err = info.value
        assert _attempts(err) == 2
        notes = getattr(err, "__notes__", [])
        assert len(notes) == 1
        assert "503" in notes[0]


# ----- schedule parity across stacks --------------------------------------


class _RecordingClock(FakeClock):
    """FakeClock that records every slept duration."""

    def __init__(self, sink: list[float]) -> None:
        super().__init__()
        self._sink = sink

    def sleep(self, duration: float) -> None:
        self._sink.append(duration)
        super().sleep(duration)


class _RecordingAsyncClock:
    """AsyncClock that records every slept duration."""

    __slots__ = ("_sink", "_t")

    def __init__(self, sink: list[float]) -> None:
        self._sink = sink
        self._t = 0.0

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    async def sleep(self, duration: float) -> None:
        self._sink.append(duration)
        self._t += max(0.0, duration)


class TestScheduleParity:
    def test_both_stacks_produce_identical_delay_schedules(self) -> None:
        # Both policies delegate to the same ``_pure/backoff`` and draw the same
        # seeded RNG in the same order, so their slept schedules are identical.
        seed = 20260714
        sync_sleeps: list[float] = []
        sync_client = _ScriptedClient([Status.SERVICE_UNAVAILABLE] * 3 + [Status.OK])
        sync_retry = RetryPolicy(
            backoff_factor=1.5,
            backoff_max=100.0,
            rand=random.Random(seed),
            clock=_RecordingClock(sync_sleeps),
        )
        with Pipeline(sync_client, policies=[sync_retry]) as p:
            p.run(_get(), DispatchContext(_instr("0" * 15 + "5e")))

        async_sleeps: list[float] = []

        async def _drive() -> None:
            async_client = _AsyncScriptedClient([Status.SERVICE_UNAVAILABLE] * 3 + [Status.OK])
            async_retry = AsyncRetryPolicy(
                backoff_factor=1.5,
                backoff_max=100.0,
                rand=random.Random(seed),
                clock=_RecordingAsyncClock(async_sleeps),
            )
            async with AsyncPipeline(async_client, policies=[async_retry]) as p:
                await p.run(_get(), DispatchContext(_instr("0" * 15 + "5f")))

        asyncio.run(_drive())

        assert sync_sleeps == async_sleeps
        # The first retry's delay is zero (attempt 1) and is skipped by the
        # bounded sleep, so two non-zero, jittered backoffs are recorded.
        assert len(sync_sleeps) == 2
        assert all(delay > 0 for delay in sync_sleeps)
