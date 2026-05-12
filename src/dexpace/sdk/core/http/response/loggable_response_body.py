"""``ResponseBody`` decorator that caches bytes for repeatable reads + logging."""
from __future__ import annotations

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
    """

    __slots__ = ("_cached", "_closed", "_drained", "_inner", "_max")

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
            raise ValueError(
                f"max_capture_bytes must be positive, got {max_capture_bytes}"
            )
        self._inner = inner
        self._max = max_capture_bytes
        self._cached: bytes = b""
        self._drained = False
        self._closed = False

    def media_type(self) -> MediaType | None:
        return self._inner.media_type()

    def content_length(self) -> int:
        return self._inner.content_length()

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        self._drain()
        view = memoryview(self._cached)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._inner.close()

    def snapshot(self) -> bytes:
        """Return an immutable copy of the captured bytes (draining if needed)."""
        self._drain()
        return self._cached

    @property
    def captured_size(self) -> int:
        return len(self._cached)

    def _drain(self) -> None:
        if self._drained:
            return
        self._drained = True
        chunks: list[bytes] = []
        captured = 0
        for chunk in self._inner.iter_bytes():
            if captured < self._max:
                take = min(self._max - captured, len(chunk))
                chunks.append(chunk[:take])
                captured += take
        self._cached = b"".join(chunks)


__all__ = ["LoggableResponseBody"]
