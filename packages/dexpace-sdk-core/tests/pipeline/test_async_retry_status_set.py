# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""End-to-end retryable-status-set wiring for ``AsyncRetryPolicy``.

The async twin reuses the sync ``RetryPolicy`` for configuration and the
per-attempt classification, so the configurable ``retry_on_status_codes`` set
must reach the async dispatch loop unchanged. These tests drive the real
``AsyncPipeline`` and assert the retry *behaviour* (attempt counts), not just
the stored set, so a regression that dropped the set on the async path would
be caught here — mirroring ``test_retry.TestCustomRetryStatusSet`` for sync.
"""

from __future__ import annotations

from collections.abc import Sequence

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
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
from dexpace.sdk.core.pipeline._pure.classifier import DEFAULT_RETRYABLE_STATUS
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


class _ScriptedAsyncClient(AsyncHttpClient):
    """Returns one status per call, in order."""

    def __init__(self, outcomes: Sequence[Status]) -> None:
        self._outcomes = list(outcomes)
        self.attempts = 0

    async def execute(self, request: Request) -> AsyncResponse:
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        return AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=outcome)


class TestAsyncCustomRetryStatusSet:
    def test_default_set_is_exactly_the_curated_codes(self) -> None:
        # The async policy defers configuration to the inner ``RetryPolicy``, so
        # its default set is the shared classifier default verbatim — the
        # permanent 501/505 stay excluded.
        retry = AsyncRetryPolicy(clock=_AsyncFakeClock())
        assert retry.config.retry_on_status_codes == DEFAULT_RETRYABLE_STATUS
        assert retry.config.retry_on_status_codes == frozenset({408, 429, 500, 502, 503, 504})
        assert 501 not in retry.config.retry_on_status_codes
        assert 505 not in retry.config.retry_on_status_codes

    async def test_custom_set_honors_explicitly_included_501(self) -> None:
        # A caller-supplied set containing the normally-permanent 501 is honored
        # on the async path: the GET is retried and eventually succeeds.
        client = _ScriptedAsyncClient([Status.NOT_IMPLEMENTED, Status.OK])
        retry = AsyncRetryPolicy(retry_on_status_codes={501}, clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_get(), DispatchContext(_instr("0" * 15 + "60")))
        assert client.attempts == 2
        assert response.status is Status.OK

    async def test_custom_set_excludes_a_default_code(self) -> None:
        # Narrowing the async set to ``{500}`` drops 503, so a 503 the default
        # set would retry is returned on the first attempt instead.
        client = _ScriptedAsyncClient([Status.SERVICE_UNAVAILABLE])
        retry = AsyncRetryPolicy(retry_on_status_codes={500}, clock=_AsyncFakeClock())
        async with AsyncPipeline(client, policies=[retry]) as p:
            response = await p.run(_get(), DispatchContext(_instr("0" * 15 + "61")))
        assert client.attempts == 1
        assert response.status is Status.SERVICE_UNAVAILABLE
