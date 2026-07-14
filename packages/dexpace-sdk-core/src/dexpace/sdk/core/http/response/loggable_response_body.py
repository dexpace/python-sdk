# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""``ResponseBody`` decorator with a two-regime capture for repeatable reads."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from typing import Final

from ..common.media_type import MediaType
from .response_body import ResponseBody, _check_chunk_size

_DEFAULT_CAP: Final[int] = (1 << 31) - 9  # CPython's effective ``bytes`` ceiling


class LoggableResponseBody(ResponseBody):
    """Wrap a ``ResponseBody`` and capture its bytes for diagnostics.

    The delegate is drained at most once, lazily, on first access — a read
    (``iter_bytes`` / ``bytes`` / ``string``), a ``snapshot``, or an ``error``
    query. Concurrent first accesses are serialised by an explicit ``Lock``
    (no GIL assumption) so the single-use delegate is consumed exactly once.

    The drain has two regimes, decided by whether the body fits the cap:

    Fits-cap (end-of-stream reached at or before ``max_capture_bytes``):
        the ENTIRE body is captured, the delegate is closed during the drain,
        and every subsequent read replays a fresh, non-consuming view of the
        cache — reads are fully repeatable.

    Exceeds-cap (more bytes remain once the cap is hit):
        only the prefix up to the cap is buffered and the delegate is left
        OPEN. The next read is served as a single-use stream that replays the
        buffered prefix and then the live tail; a second read raises
        ``RuntimeError`` because the tail is genuinely single-use once past the
        cap. Only the prefix is ever cached, so ``snapshot`` returns the prefix.

    Drain failure never silently truncates: whatever prefix was read is
    retained and the originating exception is cached. Subsequent reads re-raise
    the cached error, ``snapshot`` returns the partial bytes WITHOUT raising,
    and ``error`` surfaces the cached exception without a second drain attempt.
    ``KeyboardInterrupt`` / ``SystemExit`` propagate from the drain immediately
    (they are cached too, so later reads still refuse to replay a truncated
    read as success).

    An empty chunk yielded mid-stream is never treated as end-of-stream — only
    the iterator's own exhaustion ends the drain — so a zero-length chunk can
    never truncate the capture.
    """

    __slots__ = (
        "_cached",
        "_closed",
        "_drained",
        "_error",
        "_fully_captured",
        "_inner",
        "_inner_closed",
        "_lock",
        "_max",
        "_overflow",
        "_tail_consumed",
        "_tail_it",
    )

    def __init__(
        self,
        inner: ResponseBody,
        max_capture_bytes: int = _DEFAULT_CAP,
    ) -> None:
        """Initialise the decorator.

        Args:
            inner: The body to capture.
            max_capture_bytes: Cap on the in-memory capture. A body of this
                size or smaller is fully captured (repeatable); a larger body
                keeps only this prefix and streams the tail once.

        Raises:
            ValueError: If ``max_capture_bytes`` is non-positive.
        """
        if max_capture_bytes <= 0:
            raise ValueError(f"max_capture_bytes must be positive, got {max_capture_bytes}")
        self._inner = inner
        self._max = max_capture_bytes
        self._cached: bytes = b""
        self._overflow: bytes = b""
        self._tail_it: Iterator[bytes] | None = None
        self._error: BaseException | None = None
        self._drained = False
        self._fully_captured = False
        self._tail_consumed = False
        self._inner_closed = False
        self._closed = False
        self._lock = threading.Lock()

    def media_type(self) -> MediaType | None:
        return self._inner.media_type()

    def content_length(self) -> int:
        """Return the reported content length.

        Returns:
            The captured size once the whole body has been captured, otherwise
            the delegate's originally-declared length (``-1`` when unknown).
            Never triggers a drain.
        """
        if self._fully_captured:
            return len(self._cached)
        return self._inner.content_length()

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        _check_chunk_size(chunk_size)
        self._drain()
        if self._error is not None:
            raise self._error
        if self._fully_captured:
            return self._replay_cache(chunk_size)
        if self._tail_consumed:
            raise RuntimeError(
                "LoggableResponseBody exceeded its capture cap and is single-use "
                "past the cap; the streamed tail has already been consumed"
            )
        self._tail_consumed = True
        return self._replay_tail(chunk_size)

    def snapshot(self, max_bytes: int | None = None) -> bytes:
        """Return an immutable copy of the captured bytes (draining if needed).

        On a mid-drain failure the partial bytes read before the error are
        returned for post-mortem logging; the cached exception is not raised
        here (it surfaces from ``iter_bytes`` and ``error``). Only the buffered
        prefix is returned when the body exceeded the cap.

        Args:
            max_bytes: If given, copy at most this many bytes from the front of
                the capture. A ``memoryview`` bounds the slice so no more than
                ``max_bytes`` are ever materialised. ``None`` returns the full
                capture.

        Returns:
            The captured bytes, optionally truncated to ``max_bytes``.

        Raises:
            ValueError: If ``max_bytes`` is negative.
        """
        self._drain()
        if max_bytes is None:
            return self._cached
        if max_bytes < 0:
            raise ValueError(f"max_bytes must be non-negative, got {max_bytes}")
        return bytes(memoryview(self._cached)[:max_bytes])

    def error(self) -> BaseException | None:
        """Return the drain failure, if any.

        Triggers the one-time drain on first access, then reports the cached
        exception on every later call without a second drain attempt.

        Returns:
            The exception that aborted the drain, or ``None`` if the drain
            completed (or captured a full/partial prefix cleanly).
        """
        self._drain()
        return self._error

    @property
    def captured_size(self) -> int:
        """Bytes currently in the capture. Never exceeds ``max_capture_bytes``."""
        return len(self._cached)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_inner()

    # ----- internals ------------------------------------------------------

    def _replay_cache(self, chunk_size: int) -> Iterator[bytes]:
        """Yield a fresh, non-consuming view of the captured prefix."""
        view = memoryview(self._cached)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])

    def _replay_tail(self, chunk_size: int) -> Iterator[bytes]:
        """Yield the buffered prefix, then the overflow, then the live tail."""
        yield from self._replay_cache(chunk_size)
        if self._overflow:
            yield self._overflow
        if self._tail_it is not None:
            yield from self._tail_it

    def _close_inner(self) -> None:
        """Close the delegate once, swallowing any close failure.

        A close failure after the payload is already secured must not surface.
        ``KeyboardInterrupt`` / ``SystemExit`` still propagate.
        """
        if self._inner_closed:
            return
        self._inner_closed = True
        with contextlib.suppress(Exception):
            self._inner.close()

    def _drain(self) -> None:
        """Run the one-time drain, guarded by a double-checked lock."""
        if self._drained:
            return
        with self._lock:
            if self._drained:
                return
            try:
                self._run_drain()
            finally:
                self._drained = True

    def _run_drain(self) -> None:
        """Capture up to the cap, deciding the fits-cap vs exceeds-cap regime."""
        it = self._inner.iter_bytes()
        chunks: list[bytes] = []
        captured = 0
        try:
            for chunk in it:
                remaining = self._max - captured
                if len(chunk) <= remaining:
                    chunks.append(chunk)
                    captured += len(chunk)
                    continue
                # This chunk crosses the cap: keep the prefix, stash the tail,
                # and leave the delegate open for a single-use replay.
                chunks.append(chunk[:remaining])
                self._cached = b"".join(chunks)
                self._overflow = chunk[remaining:]
                self._tail_it = it
                return
            # End-of-stream at or before the cap: the whole body was captured.
            self._cached = b"".join(chunks)
            self._fully_captured = True
            self._close_inner()
        except BaseException as exc:
            # Cache every failure so a truncated read is never replayed as
            # success, but let ``KeyboardInterrupt`` / ``SystemExit`` propagate
            # from the drain at once (they stay cached for later reads too).
            self._error = exc
            self._cached = b"".join(chunks)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise


__all__ = ["LoggableResponseBody"]
