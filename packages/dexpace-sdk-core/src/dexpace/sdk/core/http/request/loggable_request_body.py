# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

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
            raise ValueError(f"max_capture_bytes must be positive, got {max_capture_bytes}")
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
        # Reset the tap at the START of each iteration so a replayed body
        # (retry, redirect 307) captures a single copy of the payload rather
        # than accumulating ``body + body`` across attempts. The reset is
        # eager — before the first ``next()`` — so ``snapshot`` reflects only
        # the most recent attempt even if the returned iterator is not drained.
        self._tap.seek(0)
        self._tap.truncate(0)
        return self._iter(chunk_size)

    def _iter(self, chunk_size: int) -> Iterator[bytes]:
        for chunk in self._inner.iter_bytes(chunk_size):
            remaining = self._max - self._tap.tell()
            if remaining > 0:
                self._tap.write(chunk[:remaining])
            yield chunk

    def snapshot(self, max_bytes: int | None = None) -> bytes:
        """Return an immutable copy of the captured bytes.

        Args:
            max_bytes: If given, copy at most this many bytes from the front
                of the tap. A ``memoryview`` bounds the slice so no more than
                ``max_bytes`` are ever materialised, even when the tap holds a
                large payload. ``None`` returns the full tap.

        Returns:
            The captured bytes, optionally truncated to ``max_bytes``.

        Raises:
            ValueError: If ``max_bytes`` is negative.
        """
        if max_bytes is None:
            return self._tap.getvalue()
        if max_bytes < 0:
            raise ValueError(f"max_bytes must be non-negative, got {max_bytes}")
        return bytes(self._tap.getbuffer()[:max_bytes])

    @property
    def captured_size(self) -> int:
        """Bytes currently in the tap. Capped by ``max_capture_bytes``."""
        return self._tap.tell()


__all__ = ["LoggableRequestBody"]
