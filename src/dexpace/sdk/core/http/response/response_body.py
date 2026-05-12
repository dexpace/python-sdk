"""Body of an HTTP response — abstract base plus built-in factories."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from io import BytesIO
from types import TracebackType
from typing import BinaryIO, Self

from ..common.media_type import MediaType

# Module-level alias so the in-class ``bytes()`` method does not shadow the
# built-in ``bytes`` type when annotating other methods.
_bytes = bytes


class ResponseBody(ABC):
    """Body of an HTTP response.

    Implements the context-manager protocol so the underlying transport
    handle is released deterministically::

        with response.body as body:
            payload = body.string()

    Single-use — once ``iter_bytes`` / ``bytes`` is consumed, the bytes are
    gone. Wrap with ``LoggableResponseBody`` if repeatable reads are needed.
    """

    @abstractmethod
    def media_type(self) -> MediaType | None:
        """Return the media type of the payload, or ``None`` if unknown."""

    @abstractmethod
    def content_length(self) -> int:
        """Return the number of bytes available, or ``-1`` if unknown."""

    @abstractmethod
    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Yield the body's bytes in chunks. Closes the body on exhaustion.

        Args:
            chunk_size: Suggested chunk size.

        Yields:
            Successive ``bytes`` chunks until the body is exhausted.

        Raises:
            RuntimeError: If the body has already been consumed.
        """

    @abstractmethod
    def close(self) -> None:
        """Release transport resources. Idempotent."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def bytes(self) -> _bytes:
        """Read the entire body as bytes and close the underlying stream.

        Returns:
            The full payload.
        """
        try:
            return b"".join(self.iter_bytes())
        finally:
            self.close()

    def string(self, encoding: str | None = None) -> str:
        """Read the entire body and decode it as text.

        Args:
            encoding: Override decoding; defaults to the ``charset`` parameter
                on ``media_type``, falling back to UTF-8 when unspecified.

        Returns:
            The decoded body.
        """
        if encoding is None:
            media = self.media_type()
            encoding = (media.charset if media is not None else None) or "utf-8"
        return self.bytes().decode(encoding)

    @classmethod
    def from_bytes(
        cls,
        data: _bytes,
        media_type: MediaType | None = None,
    ) -> ResponseBody:
        """Build an in-memory body. Useful for tests and replayable adapters.

        Args:
            data: Body content.
            media_type: Optional content type.

        Returns:
            A ``ResponseBody`` whose ``iter_bytes`` yields ``data``.
        """
        return _BytesResponseBody(data, media_type)

    @classmethod
    def from_stream(
        cls,
        stream: BinaryIO,
        media_type: MediaType | None = None,
        content_length: int = -1,
    ) -> ResponseBody:
        """Build a single-use body wrapping a ``BinaryIO`` stream.

        Closing the body closes the stream.

        Args:
            stream: Source stream. Ownership transfers to the body.
            media_type: Optional content type.
            content_length: Byte count if known, else ``-1``.

        Returns:
            A single-use ``ResponseBody``.
        """
        return _StreamResponseBody(stream, media_type, content_length)


class _BytesResponseBody(ResponseBody):
    """In-memory ``ResponseBody``."""

    __slots__ = ("_closed", "_consumed", "_data", "_media_type")

    def __init__(self, data: _bytes, media_type: MediaType | None) -> None:
        self._data = data
        self._media_type = media_type
        self._consumed = False
        self._closed = False

    def media_type(self) -> MediaType | None:
        return self._media_type

    def content_length(self) -> int:
        return len(self._data)

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if self._consumed:
            raise RuntimeError("ResponseBody has already been consumed")
        self._consumed = True
        view = memoryview(self._data)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])
        self.close()

    def close(self) -> None:
        self._closed = True


class _StreamResponseBody(ResponseBody):
    """Stream-backed single-use ``ResponseBody``."""

    __slots__ = ("_closed", "_consumed", "_length", "_media_type", "_stream")

    def __init__(
        self,
        stream: BinaryIO,
        media_type: MediaType | None,
        length: int,
    ) -> None:
        self._stream = stream
        self._media_type = media_type
        self._length = length
        self._consumed = False
        self._closed = False

    def media_type(self) -> MediaType | None:
        return self._media_type

    def content_length(self) -> int:
        return self._length

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if self._consumed:
            raise RuntimeError("ResponseBody has already been consumed")
        self._consumed = True
        try:
            while True:
                chunk = self._stream.read(chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream.close()


# Re-exported from ``io`` for adapter convenience.
__all__ = ["BytesIO", "ResponseBody"]
