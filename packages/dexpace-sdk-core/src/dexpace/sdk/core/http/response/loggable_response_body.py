# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""``ResponseBody`` decorator that caches bytes for repeatable reads + logging."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Final

from ..common.media_type import MediaType
from .response_body import ResponseBody

_DEFAULT_CAP: Final[int] = (1 << 31) - 9


class LoggableResponseBody(ResponseBody):
    """Wrap a ``ResponseBody`` and cache its bytes for repeatable reads.

    The first call to ``iter_bytes`` / ``bytes`` / ``string`` / ``snapshot``
    drains the underlying body into an in-memory ``bytes`` buffer (capped at
    ``max_capture_bytes``). Subsequent calls replay the cached buffer, so the
    response payload can be read repeatedly — useful for retries that need
    to log the body and then re-emit it, or for diagnostic dumps.

    Cap semantics: bytes beyond ``max_capture_bytes`` are dropped from the
    cache, but the underlying body is still fully drained and closed.

    Mid-drain failure: if the underlying body raises part-way through the
    one-time drain, the bytes read so far are retained in the cache and the
    originating exception is stored. ``iter_bytes`` re-raises that exception
    on every call so callers cannot mistake a truncated read for success,
    while ``snapshot`` still returns the partial bytes for post-mortem
    logging.

    Thread-safe first read: the one-time drain is guarded by a lock plus a
    double-checked flag so concurrent first readers consume the underlying
    single-use stream exactly once.
    """

    __slots__ = ("_cached", "_closed", "_drained", "_error", "_inner", "_lock", "_max")

    def __init__(
        self,
        inner: ResponseBody,
        max_capture_bytes: int = _DEFAULT_CAP,
    ) -> None:
        """Initialise the decorator.

        Args:
            inner: The body to cache.
            max_capture_bytes: Soft cap on the in-memory cache.

        Raises:
            ValueError: If ``max_capture_bytes`` is non-positive.
        """
        if max_capture_bytes <= 0:
            raise ValueError(f"max_capture_bytes must be positive, got {max_capture_bytes}")
        self._inner = inner
        self._max = max_capture_bytes
        self._cached: bytes = b""
        self._error: BaseException | None = None
        self._drained = False
        self._closed = False
        self._lock = threading.Lock()

    def media_type(self) -> MediaType | None:
        return self._inner.media_type()

    def content_length(self) -> int:
        return self._inner.content_length()

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        self._drain()
        if self._error is not None:
            raise self._error
        view = memoryview(self._cached)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._inner.close()

    def snapshot(self, max_bytes: int | None = None) -> bytes:
        """Return an immutable copy of the captured bytes (draining if needed).

        On a mid-drain failure the partial bytes read before the error are
        returned for post-mortem logging; the stored exception is not raised
        here (it surfaces from ``iter_bytes``).

        Args:
            max_bytes: If given, copy at most this many bytes from the front
                of the capture. A ``memoryview`` bounds the slice so no more
                than ``max_bytes`` are ever materialised. ``None`` returns the
                full capture.

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

    @property
    def captured_size(self) -> int:
        return len(self._cached)

    def _drain(self) -> None:
        if self._drained:
            return
        with self._lock:
            if self._drained:
                return
            chunks: list[bytes] = []
            captured = 0
            try:
                for chunk in self._inner.iter_bytes():
                    if captured < self._max:
                        take = min(self._max - captured, len(chunk))
                        chunks.append(chunk[:take])
                        captured += take
            except Exception as exc:
                self._error = exc
            finally:
                self._cached = b"".join(chunks)
                self._drained = True


__all__ = ["LoggableResponseBody"]
