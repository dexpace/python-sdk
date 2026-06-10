# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Resource-cleanup and deadline-propagation contracts for the retry policies.

Covers two invariants that the sync and async retry loops must share:

- A status-driven retry closes the intermediate response before sleeping, so
  the pooled connection is released instead of leaking (the success / return
  branches keep the response open — the caller owns those).
- Crossing the absolute deadline during a status-path backoff propagates
  ``ServiceResponseTimeoutError`` immediately, rather than being swallowed and
  re-entering the loop. The sync loop must match the async twin attempt-for-
  attempt.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.response_body import ResponseBody
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


class _AsyncFakeClock:
    """Deterministic ``AsyncClock`` for tests; advances time on sleep."""

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    async def sleep(self, duration: float) -> None:
        self._t += max(0.0, duration)


class _RecordingBody(ResponseBody):
    """Sync response body that records whether ``close`` was called."""

    def __init__(self) -> None:
        self.closed = False

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield b""

    def close(self) -> None:
        self.closed = True


class _AsyncRecordingBody(AsyncResponseBody):
    """Async response body that records whether ``close`` was awaited."""

    def __init__(self) -> None:
        self.closed = False

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        yield b""

    async def close(self) -> None:
        self.closed = True


class _BodyTrackingClient(HttpClient):
    """Returns responses carrying a recording body the test can inspect."""

    def __init__(self, outcomes: Sequence[Status]) -> None:
        self._outcomes = list(outcomes)
        self.bodies: list[_RecordingBody] = []
        self.attempts = 0

    def execute(self, request: Request) -> Response:
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        body = _RecordingBody()
        self.bodies.append(body)
        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=outcome,
            body=body,
        )


class _AsyncBodyTrackingClient(AsyncHttpClient):
    """Async twin of ``_BodyTrackingClient``."""

    def __init__(self, outcomes: Sequence[Status]) -> None:
        self._outcomes = list(outcomes)
        self.bodies: list[_AsyncRecordingBody] = []
        self.attempts = 0

    async def execute(self, request: Request) -> AsyncResponse:
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        body = _AsyncRecordingBody()
        self.bodies.append(body)
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=outcome,
            body=body,
        )


class _DeadlineClient(HttpClient):
    """Always returns a 503 carrying a large ``Retry-After`` header.

    The big header forces the status-path sleep to clamp to the remaining
    budget, crossing the deadline mid-backoff.
    """

    def __init__(self, retry_after: str) -> None:
        self._retry_after = retry_after
        self.attempts = 0

    def execute(self, request: Request) -> Response:
        self.attempts += 1
        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.SERVICE_UNAVAILABLE,
        ).with_header("Retry-After", self._retry_after)


class _AsyncDeadlineClient(AsyncHttpClient):
    """Async twin of ``_DeadlineClient``."""

    def __init__(self, retry_after: str) -> None:
        self._retry_after = retry_after
        self.attempts = 0

    async def execute(self, request: Request) -> AsyncResponse:
        self.attempts += 1
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.SERVICE_UNAVAILABLE,
        ).with_header("Retry-After", self._retry_after)


class TestIntermediateResponseClosed:
    def test_sync_status_retry_closes_intermediate_response(self) -> None:
        client = _BodyTrackingClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = RetryPolicy(clock=FakeClock())
        with Pipeline(client, policies=[retry]) as p:
            response = p.run(_get(), DispatchContext(_instr("0" * 16 + "1")))
        assert response.status is Status.OK
        # The intermediate 503's body was closed by the retry loop before it
        # slept, releasing the pooled connection.
        assert client.bodies[0].closed is True
        # The final response is handed back open — the caller owns it. The
        # policy must not have closed the response it returned.
        assert client.bodies[1].closed is False
        # Closing it is the caller's responsibility.
        response.close()
        assert client.bodies[1].closed is True

    async def test_async_status_retry_closes_intermediate_response(self) -> None:
        client = _AsyncBodyTrackingClient([Status.SERVICE_UNAVAILABLE, Status.OK])
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_get(), DispatchContext(_instr("0" * 16 + "2")))
        assert response.status is Status.OK
        assert client.bodies[0].closed is True
        assert client.bodies[1].closed is False
        await response.close()
        assert client.bodies[1].closed is True


class TestStatusPathDeadlinePropagates:
    """The status-path sleep must propagate a deadline timeout immediately.

    With the sleep inside the ``except SdkError`` try (the prior bug), the
    raised ``ServiceResponseTimeoutError`` — itself a ``ServiceResponseError``
    — was re-classified as a read error and the loop kept issuing requests
    until the read budget drained. Outside the try, it propagates after the
    very first attempt, matching the async twin.
    """

    def test_sync_propagates_after_single_attempt(self) -> None:
        # ``backoff_factor=0`` makes the buggy swallow-and-retry path sleep zero
        # seconds (which skips the deadline check), so the regression would keep
        # issuing requests until the read budget drained. The fix lifts the
        # status sleep out of the ``except SdkError`` try, so the timeout from
        # the very first attempt propagates straight through.
        client = _DeadlineClient(retry_after="100")
        retry = RetryPolicy(timeout=1.0, backoff_factor=0.0, clock=FakeClock())
        with (
            Pipeline(client, policies=[retry]) as p,
            pytest.raises(ServiceResponseTimeoutError),
        ):
            p.run(_get(), DispatchContext(_instr("0" * 16 + "3")))
        assert client.attempts == 1

    async def test_async_propagates_after_single_attempt(self) -> None:
        client = _AsyncDeadlineClient(retry_after="100")
        retry = AsyncRetryPolicy(timeout=1.0, backoff_factor=0.0, clock=_AsyncFakeClock())
        with pytest.raises(ServiceResponseTimeoutError):
            async with AsyncPipeline(client, policies=[retry]) as p:
                await p.run(_get(), DispatchContext(_instr("0" * 16 + "4")))
        assert client.attempts == 1

    def test_sync_attempt_count_matches_async_twin(self) -> None:
        sync_client = _DeadlineClient(retry_after="100")
        sync_retry = RetryPolicy(timeout=1.0, backoff_factor=0.0, clock=FakeClock())
        with (
            Pipeline(sync_client, policies=[sync_retry]) as p,
            pytest.raises(ServiceResponseTimeoutError),
        ):
            p.run(_get(), DispatchContext(_instr("0" * 16 + "5")))

        async_client = _AsyncDeadlineClient(retry_after="100")

        async def _drive_async() -> None:
            async_retry = AsyncRetryPolicy(
                timeout=1.0,
                backoff_factor=0.0,
                clock=_AsyncFakeClock(),
            )
            with pytest.raises(ServiceResponseTimeoutError):
                async with AsyncPipeline(async_client, policies=[async_retry]) as p:
                    await p.run(_get(), DispatchContext(_instr("0" * 16 + "6")))

        asyncio.run(_drive_async())
        assert sync_client.attempts == async_client.attempts == 1


def test_returned_response_not_closed_on_no_retry() -> None:
    """A non-retryable response is handed back open; the close fix is scoped
    to the status-retry path only."""
    client = _BodyTrackingClient([Status.OK])
    retry = RetryPolicy(clock=FakeClock())
    with Pipeline(client, policies=[retry]) as p:
        response = p.run(_get(), DispatchContext(_instr("0" * 16 + "7")))
    assert response.status is Status.OK
    # The policy did not close the response it returned to the caller.
    assert client.bodies[0].closed is False
    # The caller owns it and closes it explicitly.
    response.close()
    assert client.bodies[0].closed is True


class _AsyncBodyRecordingClient(AsyncHttpClient):
    """Async client that records the request body bytes consumed per attempt."""

    def __init__(
        self,
        outcomes: Sequence[Status | BaseException],
        consumed: list[bytes],
    ) -> None:
        self._outcomes = list(outcomes)
        self._consumed = consumed
        self.attempts = 0

    async def execute(self, request: Request) -> AsyncResponse:
        body = request.body
        captured = b"".join(body.iter_bytes()) if body is not None else b""
        self._consumed.append(captured)
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=outcome)


class _AsyncErrorClient(AsyncHttpClient):
    """Async client that raises one error or returns one status per call."""

    def __init__(self, outcomes: Sequence[Status | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.attempts = 0

    async def execute(self, request: Request) -> AsyncResponse:
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=outcome)


class TestAsyncBodyBufferingHonorsPerCallTotal:
    """The async loop keys body buffering off the effective per-call total.

    Mirrors the sync H2 contract: an ``AsyncRetryPolicy`` built with
    ``total_retries=0`` plus a per-call ``retry_total=3`` and a single-use
    body must buffer the payload for replay (drained off the event loop via
    ``asyncio.to_thread``) instead of raising ``RuntimeError`` on the second
    consumption.
    """

    async def test_per_call_retry_total_over_zero_instance_buffers_body(self) -> None:
        consumed: list[bytes] = []
        body = RequestBody.from_iter(iter([b"hello", b"world"]))
        client = _AsyncBodyRecordingClient(
            [Status.SERVICE_UNAVAILABLE, Status.SERVICE_UNAVAILABLE, Status.OK],
            consumed,
        )
        retry = AsyncRetryPolicy(total_retries=0, backoff_factor=0, clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(
                _post(body),
                DispatchContext(_instr("0" * 16 + "a")),
                retry_total=3,
            )
        assert response.status is Status.OK
        assert consumed == [b"helloworld", b"helloworld", b"helloworld"]


class TestAsyncReadPhaseMethodGating:
    """Async twin of the read-phase idempotency gating.

    A read-phase ``ServiceResponseError`` is not retried for POST (the write
    may already have landed), but a connect-phase ``ServiceRequestError`` is
    — the request never left the client.
    """

    async def test_post_not_retried_on_read_phase_error(self) -> None:
        client = _AsyncErrorClient([ServiceResponseError("connection reset"), Status.OK])
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        with pytest.raises(ServiceResponseError):
            async with AsyncPipeline(client, policies=[retry]) as p:
                await p.run(_post(), DispatchContext(_instr("0" * 16 + "b")))
        assert client.attempts == 1

    async def test_post_still_retried_on_connect_phase_error(self) -> None:
        client = _AsyncErrorClient([ServiceRequestError("dns fail"), Status.OK])
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_post(), DispatchContext(_instr("0" * 16 + "c")))
        assert response.status is Status.OK
        assert client.attempts == 2
