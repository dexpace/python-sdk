# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Cancellation discipline for ``AsyncRetryPolicy`` (P9).

``asyncio.CancelledError`` is a ``BaseException``, not an ``SdkError``, so the
policy's ``except SdkError`` clause already cannot catch it — but the policy
adds an explicit re-raise as a documented, tested invariant. These tests pin
the contract: a cancelled in-flight attempt propagates immediately and is
never retried.
"""

from __future__ import annotations

import asyncio

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import AsyncPipeline
from dexpace.sdk.core.pipeline.policies.async_retry import AsyncRetryPolicy


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


class _CancellingClient(AsyncHttpClient):
    """Raises ``CancelledError`` on the configured attempt."""

    def __init__(self, cancel_on: int = 1) -> None:
        self._cancel_on = cancel_on
        self.attempts = 0

    async def execute(self, request: Request) -> AsyncResponse:
        self.attempts += 1
        if self.attempts >= self._cancel_on:
            raise asyncio.CancelledError
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.SERVICE_UNAVAILABLE,
        )


class TestCancelledNotRetried:
    async def test_cancelled_error_propagates_immediately(self) -> None:
        client = _CancellingClient(cancel_on=1)
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        with pytest.raises(asyncio.CancelledError):
            async with AsyncPipeline(client, policies=[retry]) as p:
                await p.run(_get(), DispatchContext(_instr("0" * 16 + "1")))
        # Exactly one attempt — no retry of a cancellation.
        assert client.attempts == 1

    async def test_cancelled_after_a_retryable_failure_still_not_retried(self) -> None:
        # First attempt is a retryable 503, second attempt is cancelled.
        client = _CancellingClient(cancel_on=2)
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        with pytest.raises(asyncio.CancelledError):
            async with AsyncPipeline(client, policies=[retry]) as p:
                await p.run(_get(), DispatchContext(_instr("0" * 16 + "2")))
        # Two attempts total: the 503 was retried, the cancellation was not.
        assert client.attempts == 2

    async def test_task_cancellation_propagates_through_policy(self) -> None:
        started = asyncio.Event()

        class _HangingClient(AsyncHttpClient):
            def __init__(self) -> None:
                self.attempts = 0

            async def execute(self, request: Request) -> AsyncResponse:
                self.attempts += 1
                started.set()
                await asyncio.sleep(3600)  # never completes before cancel
                raise AssertionError("unreachable")

        client = _HangingClient()
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())

        async def _drive() -> AsyncResponse:
            async with AsyncPipeline(client, policies=[retry]) as p:
                return await p.run(_get(), DispatchContext(_instr("0" * 16 + "3")))

        task = asyncio.ensure_future(_drive())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.attempts == 1

    async def test_service_error_is_still_retried(self) -> None:
        # Guard against over-broad cancellation handling: ordinary SDK errors
        # must still flow into the retry path.
        class _FlakyClient(AsyncHttpClient):
            def __init__(self) -> None:
                self.attempts = 0

            async def execute(self, request: Request) -> AsyncResponse:
                self.attempts += 1
                if self.attempts == 1:
                    raise ServiceRequestError("connection reset")
                return AsyncResponse(
                    request=request,
                    protocol=Protocol.HTTP_1_1,
                    status=Status.OK,
                )

        client = _FlakyClient()
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_get(), DispatchContext(_instr("0" * 16 + "4")))
        assert response.status is Status.OK
        assert client.attempts == 2
