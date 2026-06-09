# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the synchronous reconnecting SSE client."""

from __future__ import annotations

import random
from collections.abc import Iterator

from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, ResponseBody, Status
from dexpace.sdk.core.http.sse.connection import SseConnection


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/stream"))


class _DropBody(ResponseBody):
    """Yields scripted chunks, then raises to simulate a mid-stream drop."""

    def __init__(self, chunks: list[bytes], *, error: BaseException | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self.closed = False

    def media_type(self) -> MediaType | None:
        return MediaType.parse("text/event-stream")

    def content_length(self) -> int:
        return -1

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield from self._chunks
        if self._error is not None:
            raise self._error

    def close(self) -> None:
        self.closed = True


def _response(body: ResponseBody | None, *, status: Status = Status.OK) -> Response:
    return Response(
        request=_request(),
        protocol=Protocol.HTTP_1_1,
        status=status,
        body=body,
    )


class _Script:
    """A send-callable returning scripted responses and recording requests."""

    def __init__(self, responses: list[Response]) -> None:
        self._responses = list(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("send called more times than scripted")
        return self._responses.pop(0)


class _RecordingClock:
    """Sync Clock that records sleep durations instead of waiting."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> float:
        return 0.0

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)


class _LowJitter(random.Random):
    """``uniform`` returns its low bound — deterministic backoff in tests."""

    def uniform(self, a: float, b: float) -> float:
        return a


def test_yields_events_and_closes_on_caller_stop() -> None:
    body = _DropBody([b"data: one\n\n", b"data: two\n\n"])
    script = _Script([_response(body)])
    conn = SseConnection(script, _request(), clock=_RecordingClock(), rand=_LowJitter())

    received: list[str] = []
    with conn as events:
        for event in events:
            received.append(event.data)
            if len(received) == 2:
                break

    assert received == ["one", "two"]
    assert body.closed is True
