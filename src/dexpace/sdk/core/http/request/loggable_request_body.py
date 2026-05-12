"""``RequestBody`` decorator that captures a copy of the bytes for logging."""
from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from typing import Final

from ..common.media_type import MediaType
from .request_body import RequestBody

_DEFAULT_CAP: Final[int] = (1 << 31) - 9  # CPython's effective ``bytes`` ceiling


class LoggableRequestBody(RequestBody):
    """Wrap a ``RequestBody`` and mirror its bytes into an in-memory tap.

    During ``iter_bytes`` every byte forwarded downstream is also written
    into an internal ``io.BytesIO``, up to a configurable cap (defaults to
    CPython's effective bytes ceiling). Call ``snapshot`` after the body has
    been iterated to obtain the captured bytes for log emission. The body's
    replayability is preserved (delegated to the underlying body).

    The capture is *additive* — emission downstream is unaffected. Concurrent
    ``iter_bytes`` is not supported (matches the underlying body's contract).
    """

    __slots__ = ("_inner", "_max", "_tap")

    def __init__(
        self,
        inner: RequestBody,
        max_capture_bytes: int = _DEFAULT_CAP,
    ) -> None:
        """Initialise the decorator.

        Args:
            inner: The body to mirror.
            max_capture_bytes: Soft cap on the in-memory tap (bytes beyond
                this are dropped from the tap but still forwarded downstream).

        Raises:
            ValueError: If ``max_capture_bytes`` is non-positive.
        """
        if max_capture_bytes <= 0:
            raise ValueError(
                f"max_capture_bytes must be positive, got {max_capture_bytes}"
            )
        self._inner = inner
        self._tap = BytesIO()
        self._max = max_capture_bytes

    @property
    def inner(self) -> RequestBody:
        return self._inner

    def media_type(self) -> MediaType | None:
        return self._inner.media_type()

    def content_length(self) -> int:
        return self._inner.content_length()

    def is_replayable(self) -> bool:
        return self._inner.is_replayable()

    def to_replayable(self) -> RequestBody:
        if self.is_replayable():
            return self
        return LoggableRequestBody(self._inner.to_replayable(), self._max)

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        for chunk in self._inner.iter_bytes(chunk_size):
            remaining = self._max - self._tap.tell()
            if remaining > 0:
                self._tap.write(chunk[:remaining])
            yield chunk

    def snapshot(self) -> bytes:
        """Return an immutable copy of the captured bytes."""
        return self._tap.getvalue()

    @property
    def captured_size(self) -> int:
        """Bytes currently in the tap. Capped by ``max_capture_bytes``."""
        return self._tap.tell()


__all__ = ["LoggableRequestBody"]
