# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Certification for the typed SSE adapter layer.

Covers `TypedSseConnection` / `AsyncTypedSseConnection`:

- SKIP discards an event and keeps pulling; DONE stops the stream and releases
  the wrapped source.
- Mapping is lazy and pull-based: exactly one event is drawn from the source per
  unit of consumer demand (plus any skipped events), never buffering ahead.
- A mapper that raises releases the wrapped source before the error propagates.
- The adapter drives the real reconnecting facade to a clean stop via DONE,
  releasing the underlying response exactly once.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Iterator

import pytest

from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, ResponseBody, Status
from dexpace.sdk.core.http.sse import (
    AsyncTypedSseConnection,
    SseConnection,
    SseSignal,
    TypedSseConnection,
)
from dexpace.sdk.core.http.sse.parser import SseEvent


class _RecordingSource:
    """A sync SSE source recording pulls and (idempotent) releases.

    Models the real `SseConnection`, whose ``close`` is idempotent: ``closes``
    counts distinct releases, so a redelegated close stays at one.
    """

    def __init__(self, events: list[SseEvent]) -> None:
        self._events = events
        self.pulled = 0
        self.closes = 0
        self._closed = False

    def __iter__(self) -> Iterator[SseEvent]:
        for event in self._events:
            self.pulled += 1
            yield event

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.closes += 1


class _AsyncRecordingSource:
    """Async twin of `_RecordingSource` (idempotent release)."""

    def __init__(self, events: list[SseEvent]) -> None:
        self._events = events
        self.pulled = 0
        self.closes = 0
        self._closed = False

    def __aiter__(self) -> AsyncIterator[SseEvent]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[SseEvent]:
        for event in self._events:
            self.pulled += 1
            yield event

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self.closes += 1


def _ev(data: str, event: str = "message") -> SseEvent:
    return SseEvent(data=data, event=event)


# --- SKIP / DONE ------------------------------------------------------------


@pytest.mark.req("SSE-24", "SSE-33", "SSE-34")
def test_skip_discards_and_keeps_pulling() -> None:
    source = _RecordingSource([_ev("keep-a"), _ev("drop"), _ev("keep-b")])

    def mapper(name: str, data: str) -> str | SseSignal:
        return SseSignal.SKIP if data == "drop" else data

    assert list(TypedSseConnection(source, mapper)) == ["keep-a", "keep-b"]
    assert source.closes == 1  # released on clean exhaustion


@pytest.mark.req("SSE-34")
def test_done_stops_the_stream_and_releases() -> None:
    source = _RecordingSource([_ev("1"), _ev("2"), _ev("stop"), _ev("3")])

    def mapper(name: str, data: str) -> int | SseSignal:
        return SseSignal.DONE if data == "stop" else int(data)

    assert list(TypedSseConnection(source, mapper)) == [1, 2]
    # Stopped at "stop": the event after it was never pulled.
    assert source.pulled == 3
    assert source.closes == 1


# --- lazy, pull-based draining ---------------------------------------------


@pytest.mark.req("SSE-35", "SSE-39")
def test_mapping_is_lazy_one_pull_per_demand() -> None:
    source = _RecordingSource([_ev("a"), _ev("b"), _ev("c")])
    typed = TypedSseConnection(source, lambda name, data: data)

    iterator = iter(typed)
    assert next(iterator) == "a"
    assert source.pulled == 1  # only the first event was drawn, nothing ahead

    assert next(iterator) == "b"
    assert source.pulled == 2

    typed.close()


@pytest.mark.req("SSE-35")
def test_lazy_draining_accounts_for_skipped_events() -> None:
    source = _RecordingSource([_ev("skip"), _ev("value"), _ev("ahead")])

    def mapper(name: str, data: str) -> str | SseSignal:
        return SseSignal.SKIP if data == "skip" else data

    typed = TypedSseConnection(source, mapper)
    iterator = iter(typed)
    assert next(iterator) == "value"
    # One demand drew exactly the skipped event plus the delivered one; the
    # third event was not pulled ahead.
    assert source.pulled == 2

    typed.close()


@pytest.mark.req("SSE-33")
def test_mapper_receives_event_name_and_data() -> None:
    source = _RecordingSource([_ev("payload", event="custom")])
    seen: list[tuple[str, str]] = []

    def mapper(name: str, data: str) -> str:
        seen.append((name, data))
        return data

    assert list(TypedSseConnection(source, mapper)) == ["payload"]
    assert seen == [("custom", "payload")]


# --- mapper failure releases the resource first ----------------------------


class _MapperError(Exception):
    pass


@pytest.mark.req("SSE-36")
def test_mapper_throw_releases_resource_first() -> None:
    source = _RecordingSource([_ev("boom")])

    def mapper(name: str, data: str) -> str:
        raise _MapperError(data)

    with pytest.raises(_MapperError):
        list(TypedSseConnection(source, mapper))

    assert source.closes == 1  # released before the mapper error propagated


# --- clean-terminal release-failure demotion (SSE-30) ----------------------


class _CloseError(Exception):
    """Raised by a source's close() to exercise release-failure handling."""


class _FailingCloseSource:
    """A sync SSE source whose close() always raises."""

    def __init__(self, events: list[SseEvent]) -> None:
        self._events = events
        self.closes = 0

    def __iter__(self) -> Iterator[SseEvent]:
        yield from self._events

    def close(self) -> None:
        self.closes += 1
        raise _CloseError("release failed")


@pytest.mark.req("SSE-30")
def test_clean_terminal_release_failure_is_swallowed() -> None:
    # A DONE sentinel is an automatic clean-terminal path with no error in
    # flight: a release failure must not be turned into a thrown result that
    # discards already-delivered events, so it is swallowed (reported
    # out-of-band), not raised.
    source = _FailingCloseSource([_ev("1"), _ev("stop")])

    def mapper(name: str, data: str) -> int | SseSignal:
        return SseSignal.DONE if data == "stop" else int(data)

    assert list(TypedSseConnection(source, mapper)) == [1]
    assert source.closes >= 1  # close was attempted despite raising


@pytest.mark.req("SSE-30")
def test_clean_exhaustion_release_failure_is_swallowed() -> None:
    # Natural end-of-stream is likewise a clean terminal: the release failure is
    # swallowed and the delivered values survive.
    source = _FailingCloseSource([_ev("a"), _ev("b")])
    assert list(TypedSseConnection(source, lambda name, data: data)) == ["a", "b"]


@pytest.mark.req("SSE-30")
def test_explicit_close_propagates_release_failure() -> None:
    # By contrast, a release failure during an EXPLICIT close() propagates to
    # the caller rather than being swallowed.
    source = _FailingCloseSource([])
    typed = TypedSseConnection(source, lambda name, data: data)

    with pytest.raises(_CloseError):
        typed.close()


class _AsyncFailingCloseSource:
    """Async twin of `_FailingCloseSource`."""

    def __init__(self, events: list[SseEvent]) -> None:
        self._events = events
        self.closes = 0

    def __aiter__(self) -> AsyncIterator[SseEvent]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[SseEvent]:
        for event in self._events:
            yield event

    async def aclose(self) -> None:
        self.closes += 1
        raise _CloseError("release failed")


@pytest.mark.req("SSE-30")
async def test_async_clean_terminal_release_failure_is_swallowed() -> None:
    source = _AsyncFailingCloseSource([_ev("1"), _ev("stop")])

    def mapper(name: str, data: str) -> int | SseSignal:
        return SseSignal.DONE if data == "stop" else int(data)

    received = [value async for value in AsyncTypedSseConnection(source, mapper)]
    assert received == [1]
    assert source.closes >= 1


@pytest.mark.req("SSE-30")
async def test_async_explicit_close_propagates_release_failure() -> None:
    source = _AsyncFailingCloseSource([])
    typed = AsyncTypedSseConnection(source, lambda name, data: data)

    with pytest.raises(_CloseError):
        await typed.aclose()


# --- integration with the real reconnecting facade -------------------------


class _Body(ResponseBody):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closes = 0

    def media_type(self) -> MediaType | None:
        return MediaType.parse("text/event-stream")

    def content_length(self) -> int:
        return -1

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.closes += 1


class _RecordingClock:
    def now(self) -> float:
        return 0.0

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, duration: float) -> None:
        return None


class _LowJitter(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return a


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/stream"))


@pytest.mark.req("SSE-37")
def test_typed_adapter_drives_real_connection_to_clean_stop() -> None:
    body = _Body([b"data: 1\n\n", b"data: 2\n\ndata: [DONE]\n\n"])

    def send(request: Request) -> Response:
        return Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.OK, body=body)

    connection = SseConnection(send, _request(), clock=_RecordingClock(), rand=_LowJitter())

    def mapper(name: str, data: str) -> int | SseSignal:
        return SseSignal.DONE if data == "[DONE]" else int(data)

    with TypedSseConnection(connection, mapper) as typed:
        assert list(typed) == [1, 2]

    assert body.closes == 1  # the underlying response was released exactly once


# --- async twin -------------------------------------------------------------


@pytest.mark.req("SSE-34")
async def test_async_skip_and_done() -> None:
    source = _AsyncRecordingSource([_ev("1"), _ev("drop"), _ev("2"), _ev("stop"), _ev("3")])

    def mapper(name: str, data: str) -> int | SseSignal:
        if data == "drop":
            return SseSignal.SKIP
        if data == "stop":
            return SseSignal.DONE
        return int(data)

    received = [value async for value in AsyncTypedSseConnection(source, mapper)]
    assert received == [1, 2]
    assert source.pulled == 4  # "3" never pulled
    assert source.closes == 1


@pytest.mark.req("SSE-36")
async def test_async_mapper_throw_releases_resource_first() -> None:
    source = _AsyncRecordingSource([_ev("boom")])

    def mapper(name: str, data: str) -> str:
        raise _MapperError(data)

    with pytest.raises(_MapperError):
        async with AsyncTypedSseConnection(source, mapper) as typed:
            async for _ in typed:
                pass

    assert source.closes == 1


@pytest.mark.req("SSE-35", "SSE-39")
async def test_async_mapping_is_lazy() -> None:
    source = _AsyncRecordingSource([_ev("a"), _ev("b"), _ev("c")])
    typed = AsyncTypedSseConnection(source, lambda name, data: data)

    iterator = aiter(typed)
    assert await anext(iterator) == "a"
    assert source.pulled == 1

    assert await anext(iterator) == "b"
    assert source.pulled == 2

    await typed.aclose()
