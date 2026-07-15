# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of ``LoggableResponseBody`` with a two-regime capture."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Final

from .._shielded import _shielded_cleanup
from ..common.media_type import MediaType
from .async_response_body import AsyncResponseBody
from .response_body import _check_chunk_size

_DEFAULT_CAP: Final[int] = (1 << 31) - 9  # CPython's effective ``bytes`` ceiling


class AsyncLoggableResponseBody(AsyncResponseBody):
    """Async twin of ``LoggableResponseBody``.

    Mirrors the sync capture contract over ``aiter_bytes``: the delegate is
    drained at most once, lazily, on first access (a read, a ``snapshot``, or an
    ``error`` query). Concurrent first accesses are serialised by an
    ``asyncio.Lock`` so the single-use delegate is consumed exactly once.

    The drain has the same two regimes as the sync body:

    Fits-cap (end-of-stream reached at or before ``max_capture_bytes``):
        the ENTIRE body is captured, the delegate is closed during the drain,
        and every subsequent read replays a fresh, non-consuming view of the
        cache — reads are fully repeatable.

    Exceeds-cap (more bytes remain once the cap is hit):
        only the prefix up to the cap is buffered and the delegate is left
        OPEN. The next read is served as a single-use stream that replays the
        buffered prefix and then the live tail; a second read raises
        ``RuntimeError``. Only the prefix is ever cached, so ``snapshot``
        returns the prefix.

    Drain failure never silently truncates: whatever prefix was read is
    retained and the originating exception is cached. Subsequent reads re-raise
    the cached error, ``snapshot`` returns the partial bytes WITHOUT raising,
    and ``error`` surfaces the cached exception without a second drain attempt.
    ``CancelledError`` from the drain is not cached — cancellation is transient
    and must propagate to the awaiting task rather than poisoning later reads.

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
        inner: AsyncResponseBody,
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
        self._tail_it: AsyncIterator[bytes] | None = None
        self._error: BaseException | None = None
        self._drained = False
        self._fully_captured = False
        self._tail_consumed = False
        self._inner_closed = False
        self._closed = False
        self._lock = asyncio.Lock()

    def media_type(self) -> MediaType | None:
        return self._inner.media_type()

    def content_length(self) -> int:
        """Return the captured size once fully captured, else the delegate's.

        Never triggers a drain: reports ``-1`` (unknown) until the body has
        been fully captured.
        """
        if self._fully_captured:
            return len(self._cached)
        return self._inner.content_length()

    def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        _check_chunk_size(chunk_size)
        return self._read(chunk_size)

    async def _read(self, chunk_size: int) -> AsyncIterator[bytes]:
        await self._drain()
        if self._error is not None:
            raise self._error
        if self._fully_captured:
            for chunk in self._replay_cache(chunk_size):
                yield chunk
            return
        if self._tail_consumed:
            raise RuntimeError(
                "AsyncLoggableResponseBody exceeded its capture cap and is single-use "
                "past the cap; the streamed tail has already been consumed"
            )
        self._tail_consumed = True
        async for chunk in self._replay_tail(chunk_size):
            yield chunk

    async def snapshot(self, max_bytes: int | None = None) -> bytes:
        """Return an immutable copy of the captured bytes (draining if needed).

        On a mid-drain failure the partial bytes read before the error are
        returned for post-mortem logging; the cached exception is not raised
        here (it surfaces from ``aiter_bytes`` and ``error``). Only the buffered
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
        await self._drain()
        if max_bytes is None:
            return self._cached
        if max_bytes < 0:
            raise ValueError(f"max_bytes must be non-negative, got {max_bytes}")
        return bytes(memoryview(self._cached)[:max_bytes])

    async def error(self) -> BaseException | None:
        """Return the drain failure, if any (draining once on first access)."""
        await self._drain()
        return self._error

    @property
    def captured_size(self) -> int:
        """Bytes currently in the capture. Never exceeds ``max_capture_bytes``."""
        return len(self._cached)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close_inner()

    # ----- internals ------------------------------------------------------

    def _replay_cache(self, chunk_size: int) -> Iterator[bytes]:
        """Yield a fresh, non-consuming view of the captured prefix."""
        view = memoryview(self._cached)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])

    async def _replay_tail(self, chunk_size: int) -> AsyncIterator[bytes]:
        """Yield the buffered prefix, then the overflow, then the live tail."""
        for chunk in self._replay_cache(chunk_size):
            yield chunk
        if self._overflow:
            yield self._overflow
        if self._tail_it is not None:
            async for chunk in self._tail_it:
                yield chunk

    async def _close_inner(self) -> None:
        """Close the delegate once, swallowing any ordinary close failure."""
        if self._inner_closed:
            return
        self._inner_closed = True
        with contextlib.suppress(Exception):
            await _shielded_cleanup(self._inner.close())

    async def _drain(self) -> None:
        """Run the one-time drain, guarded by a double-checked async lock."""
        if self._drained:
            return
        async with self._lock:
            if self._drained:
                return
            try:
                await self._run_drain()
            finally:
                self._drained = True

    async def _run_drain(self) -> None:
        """Capture up to the cap, deciding the fits-cap vs exceeds-cap regime."""
        it = self._inner.aiter_bytes()
        chunks: list[bytes] = []
        captured = 0
        try:
            async for chunk in it:
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
            self._cached = b"".join(chunks)
            self._fully_captured = True
            await self._close_inner()
        except asyncio.CancelledError:
            # Cancellation is transient: keep what was read for a post-mortem
            # snapshot but do not cache it as a terminal error, then propagate.
            self._cached = b"".join(chunks)
            raise
        except Exception as exc:
            self._error = exc
            self._cached = b"".join(chunks)


__all__ = ["AsyncLoggableResponseBody"]
