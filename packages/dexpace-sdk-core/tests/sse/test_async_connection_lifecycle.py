# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Lifecycle certification for the asynchronous SSE connection facade.

Mirrors the synchronous lifecycle guarantees for `AsyncSseConnection`:
release-exactly-once across termination paths, single-pass iteration, and a
mid-stream failure that releases before propagating without a close-time error
masking the primary failure.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator

import pytest

from dexpace.sdk.core.errors import ServiceResponseError
from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, AsyncResponseBody, Status
from dexpace.sdk.core.http.sse.connection import AsyncSseConnection


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/stream"))


class _AsyncLedgerBody(AsyncResponseBody):
    """Async response body that counts its closes."""

    def __init__(self, chunks: list[bytes], *, error: BaseException | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self.closes = 0

    def media_type(self) -> MediaType | None:
        return MediaType.parse("text/event-stream")

    def content_length(self) -> int:
        return -1

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        self.closes += 1


def _response(body: AsyncResponseBody | None, *, status: Status = Status.OK) -> AsyncResponse:
    return AsyncResponse(request=_request(), protocol=Protocol.HTTP_1_1, status=status, body=body)


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
    def now(self) -> float:
        return 0.0

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, duration: float) -> None:
        return None


class _LowJitter(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return a


def _conn(responses: list[AsyncResponse]) -> AsyncSseConnection:
    return AsyncSseConnection(
        _AsyncScript(responses), _request(), clock=_RecordingAsyncClock(), rand=_LowJitter()
    )


@pytest.mark.req("SSE-25")
async def test_release_once_on_partial_consumption() -> None:
    body = _AsyncLedgerBody([b"data: one\n\n"])
    conn = _conn([_response(body)])

    async with conn as events:
        async for event in events:
            assert event.data == "one"
            break

    assert body.closes == 1


async def test_release_once_per_response_on_clean_end_of_stream() -> None:
    first = _AsyncLedgerBody([b"data: a\n\n"])
    second = _AsyncLedgerBody([b"data: b\n\n"])
    conn = _conn([_response(first), _response(second)])

    seen: list[str] = []
    async with conn as events:
        async for event in events:
            seen.append(event.data)
            if len(seen) == 2:
                break

    assert seen == ["a", "b"]
    assert first.closes == 1
    assert second.closes == 1


async def test_release_once_on_transient_mid_stream_failure() -> None:
    dropped = _AsyncLedgerBody([b"data: a\n\n"], error=ServiceResponseError("dropped"))
    resumed = _AsyncLedgerBody([b"data: b\n\n"])
    conn = _conn([_response(dropped), _response(resumed)])

    seen: list[str] = []
    async with conn as events:
        async for event in events:
            seen.append(event.data)
            if len(seen) == 2:
                break

    assert dropped.closes == 1
    assert resumed.closes == 1


@pytest.mark.req("SSE-28")
async def test_aclose_is_idempotent() -> None:
    body = _AsyncLedgerBody([b"data: one\n\n"])
    conn = _conn([_response(body)])

    events = aiter(conn)
    assert (await anext(events)).data == "one"
    await conn.aclose()
    await conn.aclose()  # idempotent
    assert body.closes == 1


@pytest.mark.req("SSE-26")
async def test_second_iterator_raises() -> None:
    conn = _conn([_response(_AsyncLedgerBody([b"data: one\n\n"]))])
    events = aiter(conn)
    assert (await anext(events)).data == "one"

    with pytest.raises(RuntimeError, match="single iteration"):
        aiter(conn)

    await conn.aclose()


@pytest.mark.req("SSE-27")
async def test_iteration_after_close_raises() -> None:
    conn = _conn([])
    await conn.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        aiter(conn)


class _PrimaryError(Exception):
    """A non-transient mid-stream failure that must propagate."""


class _SecondaryError(Exception):
    """A failure raised while releasing; must not mask the primary failure."""


class _AsyncMaskingBody(AsyncResponseBody):
    """Fails mid-stream, then fails again on close, to test error precedence."""

    def media_type(self) -> MediaType | None:
        return MediaType.parse("text/event-stream")

    def content_length(self) -> int:
        return -1

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        raise _PrimaryError("mid-stream boom")
        yield b""  # pragma: no cover — marks this an async generator

    async def close(self) -> None:
        raise _SecondaryError("teardown failed")


@pytest.mark.req("SSE-29")
async def test_close_time_error_is_demoted_onto_context() -> None:
    conn = _conn([_response(_AsyncMaskingBody())])

    with pytest.raises(_PrimaryError) as exc_info:
        async for _ in conn:
            pass

    assert isinstance(exc_info.value.__context__, _SecondaryError)
