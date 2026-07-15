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
        self.iter_calls = 0

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return -1

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        self.iter_calls += 1
        if self._consumed:
            raise RuntimeError("ResponseBody has already been consumed")
        self._consumed = True
        return self._emit()

    def _emit(self) -> Iterator[bytes]:
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


class _ChunkedResponseBody(ResponseBody):
    """A single-use body yielding fixed chunks, with tunable metadata.

    Tracks how often it is iterated and whether it was closed. The declared
    content length and an optional ``close`` failure are configurable so the
    two-regime drain can be exercised without a real transport.
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        declared_length: int = -1,
        close_error: BaseException | None = None,
    ) -> None:
        self._chunks = chunks
        self._declared_length = declared_length
        self._close_error = close_error
        self._consumed = False
        self.closed = False
        self.iter_calls = 0

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return self._declared_length

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        self.iter_calls += 1
        if self._consumed:
            raise RuntimeError("ResponseBody has already been consumed")
        self._consumed = True
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class TestResponseErrorPath:
    @pytest.mark.req("BODY-26")
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

    @pytest.mark.req("BODY-26", "IO-27")
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


class _CustomBaseError(BaseException):
    """A non-``Exception`` ``BaseException`` raised mid-drain."""


class TestResponseBaseExceptionPath:
    def test_iter_bytes_reraises_base_exception_on_every_call(self) -> None:
        # A bare ``BaseException`` (not an ``Exception``) must still be stored
        # so a truncated read is never replayed as a complete success.
        boom = _CustomBaseError("aborted")
        inner = _FailingResponseBody([b"abc", b"def"], boom)
        body = LoggableResponseBody(inner)

        with pytest.raises(_CustomBaseError) as first:
            list(body.iter_bytes())
        assert first.value is boom

        # Second read must re-raise, not silently return the partial bytes.
        with pytest.raises(_CustomBaseError) as second:
            list(body.iter_bytes())
        assert second.value is boom

    def test_keyboard_interrupt_propagates_immediately(self) -> None:
        # KeyboardInterrupt / SystemExit must escape the drain at once rather
        # than being buffered and replayed by iter_bytes.
        inner = _FailingResponseBody([b"abc"], KeyboardInterrupt())
        body = LoggableResponseBody(inner)
        with pytest.raises(KeyboardInterrupt):
            list(body.iter_bytes())


class TestResponseThreadSafety:
    @pytest.mark.req("BODY-22")
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

    @pytest.mark.req("BODY-32")
    def test_snapshot_max_bytes_larger_than_body(self) -> None:
        inner = ResponseBody.from_bytes(b"abc")
        body = LoggableResponseBody(inner)
        assert body.snapshot(max_bytes=100) == b"abc"

    def test_snapshot_none_returns_full(self) -> None:
        inner = ResponseBody.from_bytes(b"abcde")
        body = LoggableResponseBody(inner)
        assert body.snapshot() == b"abcde"

    @pytest.mark.req("BODY-32")
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


class TestRequestTapResetOnReplay:
    """A replayed loggable request body captures one copy, not body+body."""

    @pytest.mark.req("BODY-18")
    def test_second_iteration_does_not_double_the_capture(self) -> None:
        # A replayable inner body iterated twice (retry / 307 redirect) must
        # not accumulate ``body + body`` in the tap. Each iter_bytes resets
        # the tap so snapshot/captured_size reflect a single payload.
        body = LoggableRequestBody(RequestBody.from_bytes(b"payload"))
        assert b"".join(body.iter_bytes()) == b"payload"
        assert b"".join(body.iter_bytes()) == b"payload"
        assert body.snapshot() == b"payload"
        assert body.captured_size == len(b"payload")

    @pytest.mark.req("BODY-18")
    def test_capture_reflects_only_the_latest_attempt(self) -> None:
        # After many replays the capture is still one copy, never N copies.
        body = LoggableRequestBody(RequestBody.from_bytes(b"abc"))
        for _ in range(5):
            assert b"".join(body.iter_bytes()) == b"abc"
        assert body.snapshot() == b"abc"
        assert body.captured_size == 3

    @pytest.mark.req("BODY-18", "IO-27")
    def test_tap_reset_is_eager_before_first_chunk(self) -> None:
        # The reset happens at call time, so a fresh (undrained) iterator from
        # a replay already shows an empty capture rather than the prior copy.
        body = LoggableRequestBody(RequestBody.from_bytes(b"hello"))
        list(body.iter_bytes())
        assert body.captured_size == 5
        # Obtain a new iterator without draining it; the tap must already be
        # cleared by the eager reset inside iter_bytes.
        body.iter_bytes()
        assert body.captured_size == 0
        assert body.snapshot() == b""


_CAP = 8


class TestResponseRegimeBoundary:
    """Regime detection at exactly cap-1, cap, and cap+1 bytes."""

    @pytest.mark.req("BODY-23")
    def test_below_cap_is_fully_captured_and_repeatable(self) -> None:
        data = b"x" * (_CAP - 1)
        inner = _ChunkedResponseBody([data])
        body = LoggableResponseBody(inner, max_capture_bytes=_CAP)

        assert b"".join(body.iter_bytes()) == data
        # A fully-captured body serves fresh, repeatable, non-consuming views.
        assert b"".join(body.iter_bytes()) == data
        assert body.captured_size == _CAP - 1
        # Fits-cap closes the delegate during the drain, not just on wrapper close.
        assert inner.closed is True

    def test_at_cap_is_fully_captured_and_repeatable(self) -> None:
        data = b"x" * _CAP
        inner = _ChunkedResponseBody([data])
        body = LoggableResponseBody(inner, max_capture_bytes=_CAP)

        # EOF reached exactly at the cap: the entire body fit, so reads repeat.
        assert b"".join(body.iter_bytes()) == data
        assert b"".join(body.iter_bytes()) == data
        assert body.captured_size == _CAP
        assert inner.closed is True

    @pytest.mark.req("BODY-24", "OBS-36")
    def test_above_cap_is_single_use_and_replays_prefix_then_tail(self) -> None:
        data = b"x" * (_CAP + 1)
        inner = _ChunkedResponseBody([data])
        body = LoggableResponseBody(inner, max_capture_bytes=_CAP)

        # The single read replays the buffered prefix plus the live tail.
        assert b"".join(body.iter_bytes()) == data
        # Only the prefix up to the cap is buffered.
        assert body.captured_size == _CAP
        assert body.snapshot() == b"x" * _CAP
        # A second read past the cap must fail — genuinely single-use.
        with pytest.raises(RuntimeError, match="single-use"):
            list(body.iter_bytes())


class TestResponseExceedsCapRegime:
    @pytest.mark.req("BODY-24")
    def test_drain_leaves_delegate_open_for_the_tail(self) -> None:
        inner = _ChunkedResponseBody([b"0123456789"])
        body = LoggableResponseBody(inner, max_capture_bytes=4)

        # snapshot buffers only the prefix and must NOT close the delegate:
        # the tail is still live.
        assert body.snapshot() == b"0123"
        assert inner.closed is False
        # The next read replays prefix + the remaining live tail.
        assert b"".join(body.iter_bytes()) == b"0123456789"

    def test_replays_prefix_overflow_then_tail_across_chunks(self) -> None:
        inner = _ChunkedResponseBody([b"aaaa", b"bbbb", b"cccc"])
        body = LoggableResponseBody(inner, max_capture_bytes=6)

        assert b"".join(body.iter_bytes()) == b"aaaabbbbcccc"
        # Only the 6-byte prefix is ever buffered.
        assert body.snapshot() == b"aaaabb"
        assert body.captured_size == 6


class TestResponseCloseOnce:
    @pytest.mark.req("BODY-27", "BODY-28")
    def test_full_capture_survives_failing_delegate_close(self) -> None:
        # Fits-cap closes the delegate during the drain; a close failure after
        # a successful capture is swallowed, not surfaced.
        inner = _ChunkedResponseBody([b"payload"], close_error=OSError("close boom"))
        body = LoggableResponseBody(inner)

        assert body.snapshot() == b"payload"  # no exception despite failing close
        assert inner.closed is True
        assert body.error() is None

    @pytest.mark.req("BODY-27", "BODY-28")
    def test_snapshot_still_works_after_wrapper_close(self) -> None:
        inner = _ChunkedResponseBody([b"payload"], close_error=OSError("close boom"))
        body = LoggableResponseBody(inner)

        assert body.snapshot() == b"payload"
        body.close()  # idempotent; the failing delegate close stays swallowed
        # The captured buffer must survive the wrapper's own close.
        assert body.snapshot() == b"payload"
        assert body.snapshot(max_bytes=3) == b"pay"


class TestResponseContentLengthRegime:
    @pytest.mark.req("BODY-29")
    def test_reports_captured_size_when_fully_captured(self) -> None:
        inner = _ChunkedResponseBody([b"abcdef"], declared_length=-1)
        body = LoggableResponseBody(inner)

        # Before any drain, the delegate's declared length is reported.
        assert body.content_length() == -1
        body.snapshot()  # fits-cap drain
        # After a full capture, the captured size is authoritative.
        assert body.content_length() == 6

    @pytest.mark.req("BODY-29")
    def test_reports_declared_length_when_exceeds_cap(self) -> None:
        inner = _ChunkedResponseBody([b"0123456789"], declared_length=10)
        body = LoggableResponseBody(inner, max_capture_bytes=4)

        body.snapshot()  # exceeds-cap drain: only a prefix is captured
        # Not fully captured, so the delegate's original declared length wins.
        assert body.content_length() == 10
        assert body.captured_size == 4


class TestResponseErrorAccessor:
    def test_returns_none_when_drain_succeeds(self) -> None:
        inner = _ChunkedResponseBody([b"ok"])
        body = LoggableResponseBody(inner)

        # The accessor is a first-access drain trigger.
        assert body.error() is None
        assert inner.closed is True

    @pytest.mark.req("BODY-22", "BODY-26")
    def test_surfaces_cached_error_without_a_second_drain(self) -> None:
        boom = ConnectionError("drop")
        inner = _FailingResponseBody([b"ab"], boom)
        body = LoggableResponseBody(inner)

        assert body.error() is boom
        assert inner.iter_calls == 1
        # Querying again must not re-drain the single-use delegate.
        assert body.error() is boom
        assert inner.iter_calls == 1
        # The partial prefix is still available for post-mortem logging.
        assert body.snapshot() == b"ab"


class TestResponseZeroByteNotEof:
    def test_interior_empty_chunk_is_not_end_of_stream(self) -> None:
        # An empty chunk mid-stream is a contract quirk, never EOF: only the
        # iterator's exhaustion ends the drain, so nothing is truncated.
        inner = _ChunkedResponseBody([b"ab", b"", b"cd", b"", b"ef"])
        body = LoggableResponseBody(inner)

        assert body.snapshot() == b"abcdef"
        assert b"".join(body.iter_bytes()) == b"abcdef"

    def test_interior_empty_chunk_does_not_fool_regime_detection(self) -> None:
        # Empty chunks interleaved with real data must not trip the cap check
        # into a premature exceeds-cap decision.
        inner = _ChunkedResponseBody([b"aa", b"", b"bb", b"cc"])
        body = LoggableResponseBody(inner, max_capture_bytes=4)

        assert b"".join(body.iter_bytes()) == b"aabbcc"
        assert body.snapshot() == b"aabb"


class TestRequestFullPayloadDespiteCap:
    @pytest.mark.req("BODY-17", "BODY-19", "IO-25", "IO-26")
    def test_downstream_gets_full_payload_while_tap_is_capped(self) -> None:
        # The primary write path always forwards the full payload; only the
        # in-memory tap honours the (independently configurable) cap.
        body = LoggableRequestBody(RequestBody.from_bytes(b"0123456789"), max_capture_bytes=3)

        assert b"".join(body.iter_bytes()) == b"0123456789"
        assert body.snapshot() == b"012"
        assert body.captured_size == 3
