"""Payload of an HTTP request — typed body abstractions + factories."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from typing import BinaryIO
from urllib.parse import quote

from ..common import common_media_types
from ..common.media_type import MediaType


class RequestBody(ABC):
    """Payload of an HTTP request.

    A body produces bytes on demand via ``iter_bytes``; the transport drives
    the iteration. Concrete implementations differ on whether they can be
    replayed (see ``is_replayable``) — retry logic queries that before
    deciding whether to buffer the body in memory.

    Use the classmethod factories (``from_bytes``, ``from_string``,
    ``from_form``, ``from_stream``, ``from_iter``) rather than subclassing
    for the common cases. ``FileRequestBody`` covers the file-on-disk case as
    its own concrete subclass.

    Concurrent ``iter_bytes`` on a single instance is undefined; built-in
    stream-backed bodies guard with a consumed-flag and fail loudly.
    """

    @abstractmethod
    def media_type(self) -> MediaType | None:
        """Return the media type of the payload, or ``None`` when unspecified."""

    def content_length(self) -> int:
        """Return the number of bytes ``iter_bytes`` will produce.

        Returns:
            The byte count, or ``-1`` if unknown.
        """
        return -1

    @abstractmethod
    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Yield the body's bytes in chunks of at most ``chunk_size`` bytes.

        Called once per send attempt; replayable bodies may have this method
        invoked multiple times across retries (a fresh iterator is returned).

        Args:
            chunk_size: Suggested chunk size. Implementations may yield smaller
                chunks at end-of-stream.

        Yields:
            Successive ``bytes`` chunks until the body is exhausted.
        """

    def write_to(self, stream: BinaryIO, chunk_size: int = 64 * 1024) -> int:
        """Drain the body into ``stream``.

        Convenience over ``iter_bytes`` for transports that prefer a
        ``BinaryIO`` sink. Does not close ``stream``.

        Args:
            stream: Target stream. Caller owns lifecycle.
            chunk_size: Suggested chunk size passed to ``iter_bytes``.

        Returns:
            The number of bytes written.
        """
        total = 0
        for chunk in self.iter_bytes(chunk_size):
            stream.write(chunk)
            total += len(chunk)
        return total

    def is_replayable(self) -> bool:
        """Return ``True`` when ``iter_bytes`` can be invoked more than once.

        Used by retry and body-logging to decide whether the body needs to be
        buffered before sending. Defaults to ``False``.
        """
        return False

    def to_replayable(self) -> RequestBody:
        """Return a replayable equivalent of this body.

        If ``is_replayable`` is already ``True``, returns ``self``. Otherwise
        drains ``iter_bytes`` once into an in-memory ``bytes`` buffer.

        Note:
            The original body must be considered consumed after this method
            returns.

        Returns:
            A body whose ``iter_bytes`` can be called repeatedly.
        """
        if self.is_replayable():
            return self
        return _BytesBody(b"".join(self.iter_bytes()), self.media_type())

    # ----- Factories ------------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes, media_type: MediaType | None = None) -> RequestBody:
        """Build a replayable body backed by an immutable ``bytes`` object.

        Args:
            data: Body content.
            media_type: Optional content type.

        Returns:
            A replayable ``RequestBody``.
        """
        return _BytesBody(data, media_type)

    @classmethod
    def from_string(
        cls,
        content: str,
        media_type: MediaType | None = None,
        encoding: str = "utf-8",
    ) -> RequestBody:
        """Build a replayable body backed by an encoded string.

        Args:
            content: String to encode.
            media_type: Optional content type.
            encoding: Text encoding (default UTF-8).

        Returns:
            A replayable ``RequestBody``.
        """
        return _BytesBody(content.encode(encoding), media_type)

    @classmethod
    def from_form(
        cls,
        fields: Mapping[str, str],
        encoding: str = "utf-8",
    ) -> RequestBody:
        """Build a replayable ``application/x-www-form-urlencoded`` body.

        Args:
            fields: Form fields keyed by name.
            encoding: Text encoding for the URL-encoded payload.

        Returns:
            A replayable form body carrying the standard media type.
        """
        encoded = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in fields.items()
        )
        return _BytesBody(
            encoded.encode(encoding),
            common_media_types.APPLICATION_FORM_URLENCODED,
        )

    @classmethod
    def from_stream(
        cls,
        stream: BinaryIO,
        media_type: MediaType | None = None,
        content_length: int = -1,
    ) -> RequestBody:
        """Build a single-use body backed by a ``BinaryIO`` stream.

        ``iter_bytes`` consumes and closes ``stream`` on first call; a second
        call raises ``RuntimeError``. Call ``to_replayable`` BEFORE
        ``iter_bytes`` to obtain a buffered copy for retries.

        Args:
            stream: Source stream. Ownership transfers to the body.
            media_type: Optional content type.
            content_length: Byte count if known, else ``-1``.

        Returns:
            A single-use ``RequestBody``.
        """
        return _StreamBody(stream, media_type, content_length)

    @classmethod
    def from_iter(
        cls,
        chunks: Iterable[bytes],
        media_type: MediaType | None = None,
        content_length: int = -1,
    ) -> RequestBody:
        """Build a single-use body backed by an iterable of ``bytes`` chunks.

        Args:
            chunks: Iterable to consume on first ``iter_bytes`` call.
            media_type: Optional content type.
            content_length: Byte count if known, else ``-1``.

        Returns:
            A single-use ``RequestBody``. Subsequent calls raise
            ``RuntimeError`` unless ``to_replayable`` was invoked first.
        """
        return _IterBody(chunks, media_type, content_length)


class _BytesBody(RequestBody):
    """Replayable body backed by an immutable ``bytes`` object."""

    __slots__ = ("_data", "_media_type")

    def __init__(self, data: bytes, media_type: MediaType | None) -> None:
        self._data = data
        self._media_type = media_type

    def media_type(self) -> MediaType | None:
        return self._media_type

    def content_length(self) -> int:
        return len(self._data)

    def is_replayable(self) -> bool:
        return True

    def to_replayable(self) -> RequestBody:
        return self

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        view = memoryview(self._data)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])


class _StreamBody(RequestBody):
    """Single-use body backed by a ``BinaryIO``."""

    __slots__ = ("_consumed", "_length", "_media_type", "_stream")

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

    def media_type(self) -> MediaType | None:
        return self._media_type

    def content_length(self) -> int:
        return self._length

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if self._consumed:
            raise RuntimeError(
                "RequestBody.iter_bytes was already called — the underlying "
                "stream is exhausted. Call to_replayable() BEFORE iter_bytes "
                "if retries may be needed."
            )
        self._consumed = True
        try:
            while True:
                chunk = self._stream.read(chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            self._stream.close()


class _IterBody(RequestBody):
    """Single-use body backed by an iterable of ``bytes`` chunks."""

    __slots__ = ("_chunks", "_consumed", "_length", "_media_type")

    def __init__(
        self,
        chunks: Iterable[bytes],
        media_type: MediaType | None,
        length: int,
    ) -> None:
        self._chunks = chunks
        self._media_type = media_type
        self._length = length
        self._consumed = False

    def media_type(self) -> MediaType | None:
        return self._media_type

    def content_length(self) -> int:
        return self._length

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        del chunk_size  # caller-supplied chunk size is ignored; iterator chooses its own
        if self._consumed:
            raise RuntimeError(
                "RequestBody.iter_bytes was already called — the underlying "
                "iterable is exhausted. Call to_replayable() BEFORE iter_bytes "
                "if retries may be needed."
            )
        self._consumed = True
        yield from self._chunks


__all__ = ["RequestBody"]
