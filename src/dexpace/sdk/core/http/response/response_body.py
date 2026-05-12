"""Response body — abstract base plus built-in factories."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ...io import BufferedSource
from ..common.media_type import MediaType


class ResponseBody(ABC):
    """Body of an HTTP response.

    Implements the context-manager protocol so the underlying transport handle
    is released deterministically::

        with response.body as body:
            payload = body.string()

    Single-use — once :meth:`source` is consumed the bytes are gone. Wrap with
    a logging body if you need repeatable reads.
    """

    @abstractmethod
    def media_type(self) -> Optional[MediaType]:
        """Media type of the payload, or ``None`` if unknown."""

    @abstractmethod
    def content_length(self) -> int:
        """Number of bytes available, or ``-1`` if unknown."""

    @abstractmethod
    def source(self) -> BufferedSource:
        """Return the underlying byte source. Single-use."""

    @abstractmethod
    def close(self) -> None:
        """Release transport resources. Idempotent."""

    def __enter__(self) -> "ResponseBody":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def bytes(self) -> bytes:
        """Read the entire body as bytes and close the underlying source."""
        try:
            return self.source().read_bytes()
        finally:
            self.close()

    def string(self, encoding: Optional[str] = None) -> str:
        """Read the entire body and decode it.

        ``encoding`` defaults to the ``charset`` parameter on :meth:`media_type`,
        falling back to UTF-8 when unspecified.
        """
        if encoding is None:
            media = self.media_type()
            encoding = (media.charset if media is not None else None) or "utf-8"
        return self.bytes().decode(encoding)

    @classmethod
    def from_source(
        cls,
        source: BufferedSource,
        media_type: Optional[MediaType] = None,
        content_length: int = -1,
    ) -> "ResponseBody":
        """Single-use body wrapping ``source``. Closing the body closes the source."""
        return _SourceResponseBody(source, media_type, content_length)

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        media_type: Optional[MediaType] = None,
    ) -> "ResponseBody":
        """In-memory body. Useful for tests and replayable adapters."""
        from ...io import Io

        return _SourceResponseBody(
            Io.provider.source_from_bytes(data),
            media_type,
            len(data),
        )


class _SourceResponseBody(ResponseBody):
    __slots__ = ("_source", "_media_type", "_length", "_closed")

    def __init__(
        self,
        source: BufferedSource,
        media_type: Optional[MediaType],
        length: int,
    ) -> None:
        self._source = source
        self._media_type = media_type
        self._length = length
        self._closed = False

    def media_type(self) -> Optional[MediaType]:
        return self._media_type

    def content_length(self) -> int:
        return self._length

    def source(self) -> BufferedSource:
        return self._source

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._source.close()


__all__ = ["ResponseBody"]
