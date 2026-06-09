# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the F1/F2/F3 loggable-body fixes.

F1: a mid-drain error retains the partial bytes, stores the exception, and
re-raises it from ``iter_bytes`` on every call while ``snapshot`` still
yields the partial bytes.
F2: the one-time drain is thread-safe — concurrent first readers consume the
underlying single-use body exactly once.
F3: ``snapshot(max_bytes)`` caps the copy without materialising more than
``max_bytes``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.http.common.media_type import MediaType
from dexpace.sdk.core.http.request import LoggableRequestBody, RequestBody
from dexpace.sdk.core.http.response import LoggableResponseBody
from dexpace.sdk.core.http.response.response_body import ResponseBody


class _FailingResponseBody(ResponseBody):
    """A single-use body that yields some chunks then raises mid-stream."""

    def __init__(self, chunks: list[bytes], error: BaseException) -> None:
        self._chunks = chunks
        self._error = error
        self._consumed = False
        self.closed = False

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return -1

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if self._consumed:
            raise RuntimeError("ResponseBody has already been consumed")
        self._consumed = True
        yield from self._chunks
        raise self._error

    def close(self) -> None:
        self.closed = True


class _CountingResponseBody(ResponseBody):
    """A single-use body that records how often it is iterated.

    The first chunk is delayed slightly so the drain holds its lock long
    enough for every racing reader to reach the ``_drained`` check, reliably
    exercising the double-checked-locking path.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.iter_calls = 0
        self._consumed = False

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        self.iter_calls += 1
        if self._consumed:
            raise RuntimeError("ResponseBody has already been consumed")
        self._consumed = True
        # Widen the critical section so racing readers pile up on the lock.
        threading.Event().wait(0.05)
        yield self._data

    def close(self) -> None:
        return None


class TestResponseErrorPath:
    def test_iter_bytes_reraises_stored_error_on_every_call(self) -> None:
        boom = ConnectionError("network dropped")
        inner = _FailingResponseBody([b"abc", b"def"], boom)
        body = LoggableResponseBody(inner)

        with pytest.raises(ConnectionError) as first:
            list(body.iter_bytes())
        assert first.value is boom

        # Re-raises on every subsequent call, not just the first.
        with pytest.raises(ConnectionError) as second:
            list(body.iter_bytes())
        assert second.value is boom

    def test_snapshot_returns_partial_bytes_not_empty(self) -> None:
        inner = _FailingResponseBody([b"abc", b"def"], ConnectionError("drop"))
        body = LoggableResponseBody(inner)

        # snapshot must surface the partial read for post-mortem logging.
        assert body.snapshot() == b"abcdef"
        assert body.captured_size == 6

    def test_snapshot_partial_then_iter_still_raises(self) -> None:
        boom = ConnectionError("drop")
        inner = _FailingResponseBody([b"xy"], boom)
        body = LoggableResponseBody(inner)

        assert body.snapshot() == b"xy"
        with pytest.raises(ConnectionError) as exc:
            list(body.iter_bytes())
        assert exc.value is boom

    def test_bounded_snapshot_caps_partial_bytes(self) -> None:
        inner = _FailingResponseBody([b"abcdef"], ConnectionError("drop"))
        body = LoggableResponseBody(inner)
        assert body.snapshot(max_bytes=3) == b"abc"


class TestResponseThreadSafety:
    def test_concurrent_first_read_drains_exactly_once(self) -> None:
        threads_count = 8
        start = threading.Barrier(threads_count)
        inner = _CountingResponseBody(b"payload")
        body = LoggableResponseBody(inner)

        results: list[bytes] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def reader() -> None:
            # All readers fire iter_bytes at the same instant.
            start.wait(timeout=5)
            try:
                data = b"".join(body.iter_bytes())
            except BaseException as exc:
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    results.append(data)

        threads = [threading.Thread(target=reader) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert errors == []
        assert inner.iter_calls == 1
        assert results == [b"payload"] * threads_count


class TestResponseBoundedSnapshot:
    def test_snapshot_caps_copy(self) -> None:
        inner = ResponseBody.from_bytes(b"0123456789")
        body = LoggableResponseBody(inner)
        assert body.snapshot(max_bytes=4) == b"0123"

    def test_snapshot_max_bytes_larger_than_body(self) -> None:
        inner = ResponseBody.from_bytes(b"abc")
        body = LoggableResponseBody(inner)
        assert body.snapshot(max_bytes=100) == b"abc"

    def test_snapshot_none_returns_full(self) -> None:
        inner = ResponseBody.from_bytes(b"abcde")
        body = LoggableResponseBody(inner)
        assert body.snapshot() == b"abcde"

    def test_snapshot_negative_max_bytes_rejected(self) -> None:
        inner = ResponseBody.from_bytes(b"abc")
        body = LoggableResponseBody(inner)
        with pytest.raises(ValueError, match="non-negative"):
            body.snapshot(max_bytes=-1)


class TestRequestBoundedSnapshot:
    def test_snapshot_caps_copy(self) -> None:
        body = LoggableRequestBody(RequestBody.from_bytes(b"0123456789"))
        list(body.iter_bytes())
        assert body.snapshot(max_bytes=4) == b"0123"

    def test_snapshot_max_bytes_larger_than_tap(self) -> None:
        body = LoggableRequestBody(RequestBody.from_bytes(b"abc"))
        list(body.iter_bytes())
        assert body.snapshot(max_bytes=100) == b"abc"

    def test_snapshot_none_returns_full(self) -> None:
        body = LoggableRequestBody(RequestBody.from_bytes(b"abcde"))
        list(body.iter_bytes())
        assert body.snapshot() == b"abcde"

    def test_snapshot_negative_max_bytes_rejected(self) -> None:
        body = LoggableRequestBody(RequestBody.from_bytes(b"abc"))
        list(body.iter_bytes())
        with pytest.raises(ValueError, match="non-negative"):
            body.snapshot(max_bytes=-1)

    def test_snapshot_after_cap_does_not_block_further_writes(self) -> None:
        # getbuffer() must be released before more writes; a follow-up
        # snapshot proves the temporary view did not pin the BytesIO.
        body = LoggableRequestBody(RequestBody.from_bytes(b"0123456789"))
        list(body.iter_bytes())
        assert body.snapshot(max_bytes=2) == b"01"
        assert body.snapshot() == b"0123456789"
