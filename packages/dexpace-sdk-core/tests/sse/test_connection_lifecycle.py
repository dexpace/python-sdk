# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Lifecycle certification for the synchronous SSE connection facade.

Covers the resource-ownership contract the facade must uphold regardless of how
a stream ends:

- The owned response is released **exactly once** across every termination path
  (clean end-of-stream, an explicit ``close``, exiting a ``with`` block, a
  caller that stops iterating early, and a mid-stream failure).
- ``close`` is idempotent and safe to call from another thread while iteration
  is in progress, with an explicit lock guarding the close-once flag (no
  GIL-atomicity assumption, so the guarantee survives free-threaded CPython).
- The facade is single-pass: a second iterator, or iterating after ``close``,
  raises rather than silently opening a fresh request.
- A mid-stream failure releases the resource before propagating, and a
  close-time secondary error is demoted onto ``__context__`` rather than masking
  the primary failure.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.errors import ServiceResponseError
from dexpace.sdk.core.http.common import MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, ResponseBody, Status
from dexpace.sdk.core.http.sse.connection import SseConnection


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/stream"))


class _LedgerBody(ResponseBody):
    """Records how many times it is closed, to prove release-exactly-once."""

    def __init__(self, chunks: list[bytes], *, error: BaseException | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self.closes = 0

    def media_type(self) -> MediaType | None:
        return MediaType.parse("text/event-stream")

    def content_length(self) -> int:
        return -1

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield from self._chunks
        if self._error is not None:
            raise self._error

    def close(self) -> None:
        self.closes += 1


def _response(body: ResponseBody | None, *, status: Status = Status.OK) -> Response:
    return Response(request=_request(), protocol=Protocol.HTTP_1_1, status=status, body=body)


class _Script:
    """A send-callable returning scripted responses; extra calls fail loudly."""

    def __init__(self, responses: list[Response]) -> None:
        self._responses = list(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("send called more times than scripted")
        return self._responses.pop(0)


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


def _conn(responses: list[Response]) -> SseConnection:
    return SseConnection(_Script(responses), _request(), clock=_RecordingClock(), rand=_LowJitter())


# --- release-exactly-once ledger across every termination path -------------


@pytest.mark.req("SSE-25")
def test_release_once_on_partial_consumption() -> None:
    body = _LedgerBody([b"data: one\n\n"])
    conn = _conn([_response(body)])

    for event in conn:
        assert event.data == "one"
        break  # caller stops iterating early

    assert body.closes == 1


@pytest.mark.req("SSE-23", "SSE-25")
def test_release_once_on_with_block_exit() -> None:
    body = _LedgerBody([b"data: one\n\n"])

    with _conn([_response(body)]) as conn:
        it = iter(conn)
        assert next(it).data == "one"  # parked mid-stream, response still owned
    # ``__exit__`` must release the still-open response exactly once.
    assert body.closes == 1


@pytest.mark.req("SSE-23", "SSE-28")
def test_release_once_and_idempotent_on_explicit_close() -> None:
    body = _LedgerBody([b"data: one\n\n"])
    conn = _conn([_response(body)])

    it = iter(conn)
    assert next(it).data == "one"
    conn.close()
    conn.close()  # idempotent — a second call must not double-release
    assert body.closes == 1


@pytest.mark.req("SSE-17", "SSE-24")
def test_release_once_per_response_on_clean_end_of_stream() -> None:
    # A clean end-of-stream reconnects; the finished response must be released
    # exactly once as the client rolls over to the next connection.
    first = _LedgerBody([b"data: a\n\n"])
    second = _LedgerBody([b"data: b\n\n"])
    conn = _conn([_response(first), _response(second)])

    seen: list[str] = []
    for event in conn:
        seen.append(event.data)
        if len(seen) == 2:
            break

    assert seen == ["a", "b"]
    assert first.closes == 1
    assert second.closes == 1


@pytest.mark.req("SSE-23", "SSE-28")
def test_release_once_on_transient_mid_stream_failure() -> None:
    dropped = _LedgerBody([b"data: a\n\n"], error=ServiceResponseError("dropped"))
    resumed = _LedgerBody([b"data: b\n\n"])
    conn = _conn([_response(dropped), _response(resumed)])

    seen: list[str] = []
    for event in conn:
        seen.append(event.data)
        if len(seen) == 2:
            break

    assert seen == ["a", "b"]
    assert dropped.closes == 1
    assert resumed.closes == 1


class _PrimaryError(Exception):
    """A non-transient mid-stream failure that must propagate to the caller."""


class _SecondaryError(Exception):
    """A failure raised while releasing; must not mask the primary failure."""


@pytest.mark.req("SSE-29")
def test_non_transient_failure_releases_before_propagating() -> None:
    body = _LedgerBody([], error=_PrimaryError("mid-stream boom"))
    conn = _conn([_response(body)])

    with pytest.raises(_PrimaryError):
        for _ in conn:
            pass

    assert body.closes == 1  # released before the exception surfaced


class _MaskingBody(ResponseBody):
    """Fails mid-stream, then fails again on close, to test error precedence."""

    def media_type(self) -> MediaType | None:
        return MediaType.parse("text/event-stream")

    def content_length(self) -> int:
        return -1

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        raise _PrimaryError("mid-stream boom")
        yield b""  # pragma: no cover — marks this a generator

    def close(self) -> None:
        raise _SecondaryError("teardown failed")


@pytest.mark.req("SSE-29")
def test_close_time_error_is_demoted_onto_context() -> None:
    conn = _conn([_response(_MaskingBody())])

    with pytest.raises(_PrimaryError) as exc_info:
        for _ in conn:
            pass

    # The primary mid-stream failure wins; the close-time error is preserved on
    # __context__ rather than masking it.
    assert isinstance(exc_info.value.__context__, _SecondaryError)


# --- single-pass iteration -------------------------------------------------


@pytest.mark.req("SSE-26")
def test_second_iterator_raises() -> None:
    conn = _conn([_response(_LedgerBody([b"data: one\n\n"]))])
    it = iter(conn)
    assert next(it).data == "one"

    with pytest.raises(RuntimeError, match="single iteration"):
        iter(conn)

    conn.close()


@pytest.mark.req("SSE-27")
def test_iteration_after_close_raises() -> None:
    conn = _conn([])
    conn.close()

    with pytest.raises(RuntimeError, match="closed"):
        iter(conn)


# --- cross-thread close race ------------------------------------------------


class _ParkingBody(ResponseBody):
    """Yields one event, parks until ``close`` wakes it, then ends cleanly.

    ``close`` is the only thing that sets ``unblock``; counting its invocations
    proves that a race of concurrent closers releases exactly once.
    """

    def __init__(self, parked: threading.Event, unblock: threading.Event) -> None:
        self._parked = parked
        self._unblock = unblock
        self.closes = 0

    def media_type(self) -> MediaType | None:
        return MediaType.parse("text/event-stream")

    def content_length(self) -> int:
        return -1

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield b"data: one\n\n"
        self._parked.set()
        assert self._unblock.wait(5.0)  # park like a live stream awaiting bytes

    def close(self) -> None:
        self.closes += 1
        self._unblock.set()


@pytest.mark.req("SSE-18", "SSE-27", "SSE-31")
def test_close_from_another_thread_during_iteration_releases_once() -> None:
    # Free-threaded-CPython correctness: the close-once transition is guarded by
    # an explicit ``threading.Lock`` (not the GIL), so only the closer that
    # atomically takes the response slot calls ``close`` on it — even with three
    # closers firing simultaneously and the reader parked in the transport. On a
    # no-GIL build without the lock, the check-then-act on the slot could let
    # several closers double-release; this test pins that it cannot.
    parked = threading.Event()
    unblock = threading.Event()
    body = _ParkingBody(parked, unblock)
    conn = _conn([_response(body)])

    seen: list[str] = []
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            for event in conn:
                seen.append(event.data)
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=consume)
    reader.start()
    assert parked.wait(5.0)  # reader is now parked mid-stream

    start = threading.Barrier(3)

    def close_racing() -> None:
        start.wait(5.0)
        conn.close()

    closers = [threading.Thread(target=close_racing) for _ in range(3)]
    for thread in closers:
        thread.start()
    for thread in closers:
        thread.join(5.0)
    reader.join(5.0)

    assert not reader.is_alive()
    assert body.closes == 1  # released exactly once despite three concurrent closers
    assert seen == ["one"]
    assert errors == []  # the parked iteration ended cleanly, not corrupted
