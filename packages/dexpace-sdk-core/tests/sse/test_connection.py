# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the synchronous reconnecting SSE client."""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.errors import HttpResponseError, ServiceResponseError
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


class _HighJitter(random.Random):
    """``uniform`` returns its high bound — exercises upward jitter."""

    def uniform(self, a: float, b: float) -> float:
        return b


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


def test_reconnects_after_mid_stream_drop_and_replays_last_event_id() -> None:
    dropped = _DropBody([b"id: 7\ndata: one\n\n"], error=ServiceResponseError("dropped"))
    resumed = _DropBody([b"data: two\n\n"])
    script = _Script([_response(dropped), _response(resumed)])
    conn = SseConnection(script, _request(), clock=_RecordingClock(), rand=_LowJitter())

    received: list[str] = []
    with conn as events:
        for event in events:
            received.append(event.data)
            if len(received) == 2:
                break

    assert received == ["one", "two"]
    # Second request resumed from the last seen id.
    assert script.requests[1].headers.get("last-event-id") == "7"


def test_reconnects_after_clean_eof() -> None:
    first = _DropBody([b"data: a\n\n"])
    second = _DropBody([b"data: b\n\n"])
    script = _Script([_response(first), _response(second)])
    conn = SseConnection(script, _request(), clock=_RecordingClock(), rand=_LowJitter())

    received: list[str] = []
    with conn as events:
        for event in events:
            received.append(event.data)
            if len(received) == 2:
                break

    assert received == ["a", "b"]
    assert first.closed is True


def test_honours_server_retry_then_exponential_backoff() -> None:
    # First stream sets retry: 1000ms then drops with no events after it; each
    # subsequent stream also yields nothing and drops, so failures accumulate.
    def drop() -> Response:
        return _response(_DropBody([], error=ServiceResponseError("x")))

    retry_then_drop = _response(_DropBody([b"retry: 1000\n\n"], error=ServiceResponseError("x")))
    script = _Script([retry_then_drop, drop(), drop()])
    clock = _RecordingClock()
    conn = SseConnection(script, _request(), clock=clock, rand=_LowJitter(), max_reconnects=2)

    with pytest.raises(ServiceResponseError, match="reconnect budget"):
        for _ in conn:
            pass

    # base = 1.0s (from retry:), failures 0 and 1 -> [1.0, 2.0]; then bound hit.
    assert clock.sleeps == [1.0, 2.0]


def test_failure_counter_resets_after_progress() -> None:
    # A stream that yields an event resets the backoff exponent: the next
    # reconnect delay returns to the base rather than growing.
    progress = _response(_DropBody([b"data: x\n\n"], error=ServiceResponseError("x")))
    again = _response(_DropBody([b"data: y\n\n"]))
    script = _Script([progress, again])
    clock = _RecordingClock()
    conn = SseConnection(script, _request(), clock=clock, rand=_LowJitter())

    received: list[str] = []
    with conn as events:
        for event in events:
            received.append(event.data)
            if len(received) == 2:
                break

    # Both connections progressed, so the single reconnect slept the base 3.0s.
    assert clock.sleeps == [3.0]


def test_non_success_status_raises_without_reconnect() -> None:
    script = _Script([_response(None, status=Status.NOT_FOUND)])
    conn = SseConnection(script, _request(), clock=_RecordingClock(), rand=_LowJitter())

    with pytest.raises(HttpResponseError):
        for _ in conn:
            pass

    # No reconnect attempted: send called exactly once.
    assert len(script.requests) == 1


def test_empty_id_clears_replay_header() -> None:
    # id:5 then an explicit empty id (clears), so the reconnect omits the header.
    first = _DropBody([b"id: 5\ndata: a\n\nid:\ndata: b\n\n"], error=ServiceResponseError("x"))
    second = _DropBody([b"data: c\n\n"])
    script = _Script([_response(first), _response(second)])
    conn = SseConnection(script, _request(), clock=_RecordingClock(), rand=_LowJitter())

    received: list[str] = []
    with conn as events:
        for event in events:
            received.append(event.data)
            if len(received) == 3:
                break

    assert received == ["a", "b", "c"]
    assert script.requests[1].headers.get("last-event-id") is None


def test_backoff_jitter_is_upward() -> None:
    first = _DropBody([b"data: a\n\n"], error=ServiceResponseError("x"))
    second = _DropBody([b"data: b\n\n"])
    script = _Script([_response(first), _response(second)])
    clock = _RecordingClock()
    conn = SseConnection(script, _request(), clock=clock, rand=_HighJitter(), jitter=0.1)

    received: list[str] = []
    with conn as events:
        for event in events:
            received.append(event.data)
            if len(received) == 2:
                break

    # First connection progressed -> failures reset to 0 -> base 3.0; high
    # jitter multiplies by 1.0 + 0.1, never below the base.
    assert clock.sleeps == [pytest.approx(3.3)]
    assert clock.sleeps[0] > 3.0


def test_budget_exhaustion_chains_last_transport_error() -> None:
    boom = ServiceResponseError("connection reset")
    script = _Script(
        [
            _response(_DropBody([], error=boom)),
            _response(_DropBody([], error=boom)),
        ]
    )
    conn = SseConnection(
        script, _request(), clock=_RecordingClock(), rand=_LowJitter(), max_reconnects=1
    )

    with pytest.raises(ServiceResponseError, match="reconnect budget") as exc_info:
        for _ in conn:
            pass
    assert exc_info.value.__cause__ is boom
