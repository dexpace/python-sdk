# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the asynchronous reconnecting SSE client."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator

import pytest

from dexpace.sdk.core.errors import HttpResponseError, ServiceResponseError
from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, AsyncResponseBody, Status
from dexpace.sdk.core.http.sse.connection import AsyncSseConnection


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/stream"))


class _AsyncDropBody(AsyncResponseBody):
    """Yields scripted chunks, optionally then raises, optionally blocks."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        error: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self._chunks = chunks
        self._error = error
        self._block = block
        self.closed = False

    def media_type(self) -> MediaType | None:
        return MediaType.parse("text/event-stream")

    def content_length(self) -> int:
        return -1

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._block:
            await asyncio.Event().wait()  # park until cancelled
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        self.closed = True


def _response(body: AsyncResponseBody | None, *, status: Status = Status.OK) -> AsyncResponse:
    return AsyncResponse(
        request=_request(),
        protocol=Protocol.HTTP_1_1,
        status=status,
        body=body,
    )


class _AsyncScript:
    def __init__(self, responses: list[AsyncResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[Request] = []

    async def __call__(self, request: Request) -> AsyncResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("send called more times than scripted")
        return self._responses.pop(0)


class _RecordingAsyncClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> float:
        return 0.0

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)


class _LowJitter(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return a


async def test_async_yields_events_and_reconnects_with_replay() -> None:
    dropped = _AsyncDropBody([b"id: 9\ndata: one\n\n"], error=ServiceResponseError("drop"))
    resumed = _AsyncDropBody([b"data: two\n\n"])
    script = _AsyncScript([_response(dropped), _response(resumed)])
    conn = AsyncSseConnection(script, _request(), clock=_RecordingAsyncClock(), rand=_LowJitter())

    received: list[str] = []
    async with conn as events:
        async for event in events:
            received.append(event.data)
            if len(received) == 2:
                break

    assert received == ["one", "two"]
    assert script.requests[1].headers.get("last-event-id") == "9"


async def test_async_non_success_status_raises() -> None:
    script = _AsyncScript([_response(None, status=Status.BAD_GATEWAY)])
    conn = AsyncSseConnection(script, _request(), clock=_RecordingAsyncClock(), rand=_LowJitter())

    with pytest.raises(HttpResponseError):
        async for _ in conn:
            pass
    assert len(script.requests) == 1


async def test_async_cancellation_propagates_and_closes() -> None:
    body = _AsyncDropBody([b"data: hi\n\n"], block=True)
    script = _AsyncScript([_response(body)])
    conn = AsyncSseConnection(script, _request(), clock=_RecordingAsyncClock(), rand=_LowJitter())

    seen: list[str] = []

    async def consume() -> None:
        async with conn as events:
            async for event in events:
                seen.append(event.data)

    task = asyncio.ensure_future(consume())
    for _ in range(10):
        await asyncio.sleep(0)
        if seen:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == ["hi"]
    assert body.closed is True
