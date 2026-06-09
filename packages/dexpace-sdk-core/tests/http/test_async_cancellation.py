# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Cancellation discipline for async response bodies and the SSE stream.

These tests cancel a task mid-read / mid-stream and assert two invariants of
the shielded-cleanup convention (P9):

- the underlying transport handle is released (``close`` / ``aclose`` runs to
  completion), and
- ``asyncio.CancelledError`` continues to propagate — cleanup never swallows
  it.
"""

from __future__ import annotations

import asyncio

import pytest

from dexpace.sdk.core.http.response import AsyncResponse, AsyncResponseBody
from dexpace.sdk.core.http.sse.parser import parse_async_events


class _SlowStream:
    """Async stream whose ``read`` blocks and whose ``close`` is slow.

    ``read`` parks on an event so a consumer can be cancelled while awaiting
    it. ``close`` awaits a short sleep so the test can observe whether the
    close ran to completion under cancellation rather than being interrupted.
    """

    def __init__(self, *, payload: bytes = b"chunk") -> None:
        self._payload = payload
        self._gate = asyncio.Event()
        self.closed = False
        self.close_completed = False

    async def read(self, size: int = -1) -> bytes:
        await self._gate.wait()
        return self._payload

    async def close(self) -> object:
        self.closed = True
        # A real transport close yields to the loop; make sure the await
        # completes even though the enclosing task was cancelled.
        await asyncio.sleep(0)
        self.close_completed = True
        return None


async def test_aiter_bytes_releases_stream_and_propagates_when_cancelled_mid_read() -> None:
    stream = _SlowStream()
    body = AsyncResponseBody.from_async_stream(stream)

    async def consume() -> None:
        async for _ in body.aiter_bytes():
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)  # let the task reach the blocking read
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed is True
    assert stream.close_completed is True


async def test_response_aexit_releases_body_when_cancelled() -> None:
    stream = _SlowStream()
    body = AsyncResponseBody.from_async_stream(stream)
    from dexpace.sdk.core.http.common.protocol import Protocol
    from dexpace.sdk.core.http.common.url import Url
    from dexpace.sdk.core.http.request import Method, Request
    from dexpace.sdk.core.http.response import Status

    request = Request(method=Method.GET, url=Url.parse("https://example.test/"))
    response = AsyncResponse(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=Status.OK,
        body=body,
    )

    async def consume() -> None:
        async with response:
            async for _ in body.aiter_bytes():
                pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed is True
    assert stream.close_completed is True


async def test_close_runs_to_completion_under_direct_cancellation() -> None:
    """A scope cancelled while awaiting ``close`` still releases the stream."""
    stream = _SlowStream()
    body = AsyncResponseBody.from_async_stream(stream)
    # Consume nothing; close directly inside a task that is then cancelled.

    started = asyncio.Event()

    async def closer() -> None:
        started.set()
        await body.close()

    task = asyncio.ensure_future(closer())
    await started.wait()
    await asyncio.sleep(0)  # let close() reach its inner await
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed is True
    assert stream.close_completed is True


class _SlowSseChunks:
    """Async byte iterator for SSE whose ``aclose`` is slow and observable."""

    def __init__(self) -> None:
        self._gate = asyncio.Event()
        self._sent = False
        self.aclosed = False
        self.aclose_completed = False

    def __aiter__(self) -> _SlowSseChunks:
        return self

    async def __anext__(self) -> bytes:
        if not self._sent:
            self._sent = True
            return b"data: first\n\n"
        await self._gate.wait()  # block forever until cancelled
        return b""

    async def aclose(self) -> None:
        self.aclosed = True
        await asyncio.sleep(0)
        self.aclose_completed = True


async def test_sse_stream_releases_upstream_and_propagates_when_cancelled_mid_stream() -> None:
    chunks = _SlowSseChunks()
    stream = parse_async_events(chunks)
    seen: list[str] = []

    async def consume() -> None:
        async with stream:
            async for event in stream:
                seen.append(event.data)

    task = asyncio.ensure_future(consume())
    # Pump the loop until the first event is consumed and the iterator blocks.
    for _ in range(10):
        await asyncio.sleep(0)
        if seen:
            break
    assert seen == ["first"]
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert chunks.aclosed is True
    assert chunks.aclose_completed is True


async def test_sse_aclose_is_idempotent() -> None:
    chunks = _SlowSseChunks()
    stream = parse_async_events(chunks)
    await stream.aclose()
    await stream.aclose()
    assert chunks.aclosed is True
    assert chunks.aclose_completed is True
