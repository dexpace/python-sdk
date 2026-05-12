"""Request body — abstract base plus built-in factories."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping, Optional
from urllib.parse import quote

from ...io import Buffer, BufferedSink, BufferedSource, Io, IoProvider
from ..common import common_media_types
from ..common.media_type import MediaType


class RequestBody(ABC):
    """Payload of an HTTP request.

    A body produces bytes on demand via :meth:`write_to`; the transport drives
    the write. Concrete implementations differ on whether they can be replayed
    (see :meth:`is_replayable`) — retry logic queries that before deciding
    whether to buffer the body in memory.

    Use the classmethod factories (:meth:`from_bytes`, :meth:`from_string`,
    :meth:`from_form`, :meth:`from_buffer`, :meth:`from_source`) rather than
    subclassing for the common cases.

    Concurrent :meth:`write_to` on a single instance is undefined; built-in
    stream-backed bodies guard with a consumed-flag and fail loudly.
    """

    @abstractmethod
    def media_type(self) -> Optional[MediaType]:
        """Media type of the payload, or ``None`` when unspecified."""

    def content_length(self) -> int:
        """Number of bytes :meth:`write_to` will produce, or ``-1`` if unknown."""
        return -1

    @abstractmethod
    def write_to(self, sink: BufferedSink) -> None:
        """Write the body to ``sink``.

        Called once per send attempt; replayable bodies may have this method
        invoked multiple times across retries.
        """

    def is_replayable(self) -> bool:
        """True when :meth:`write_to` can be invoked more than once.

        Used by retry and body-logging to decide whether the body needs to be
        buffered before sending. Defaults to ``False``.
        """
        return False

    def to_replayable(self, provider: Optional[IoProvider] = None) -> "RequestBody":
        """Return a replayable equivalent of this body.

        If :meth:`is_replayable` is already ``True``, returns ``self``.
        Otherwise drains :meth:`write_to` once into an in-memory buffer obtained
        from ``provider`` (defaulting to :attr:`Io.provider`).

        The original body must be considered consumed after this method returns.
        """
        if self.is_replayable():
            return self
        buf = (provider or Io.provider).buffer()
        self.write_to(buf)
        return _BufferBody(buf, self.media_type(), buf.size)

    # ----- Factories ------------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes, media_type: Optional[MediaType] = None) -> "RequestBody":
        """Replayable body backed by an immutable ``bytes`` object."""
        return _BytesBody(data, media_type)

    @classmethod
    def from_string(
        cls,
        content: str,
        media_type: Optional[MediaType] = None,
        encoding: str = "utf-8",
    ) -> "RequestBody":
        """Replayable body backed by ``content`` encoded with ``encoding``."""
        return _BytesBody(content.encode(encoding), media_type)

    @classmethod
    def from_form(
        cls,
        fields: Mapping[str, str],
        encoding: str = "utf-8",
    ) -> "RequestBody":
        """Replayable ``application/x-www-form-urlencoded`` body."""
        encoded = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in fields.items()
        )
        return _BytesBody(
            encoded.encode(encoding),
            common_media_types.APPLICATION_FORM_URLENCODED,
        )

    @classmethod
    def from_buffer(
        cls,
        buffer: Buffer,
        media_type: Optional[MediaType] = None,
    ) -> "RequestBody":
        """Replayable body backed by an in-memory :class:`Buffer`.

        Reads via a non-consuming ``peek()`` on every :meth:`write_to`, so the
        body can be sent any number of times.
        """
        return _BufferBody(buffer, media_type, buffer.size)

    @classmethod
    def from_source(
        cls,
        source: BufferedSource,
        media_type: Optional[MediaType] = None,
        content_length: int = -1,
    ) -> "RequestBody":
        """Single-use body backed by a :class:`BufferedSource`.

        :meth:`write_to` drains and closes the source on first call; a second
        call raises :class:`RuntimeError`. Call :meth:`to_replayable` BEFORE
        :meth:`write_to` to obtain a buffered copy for retries.
        """
        return _SourceBody(source, media_type, content_length)


class _BytesBody(RequestBody):
    """Replayable body backed by an immutable ``bytes`` object."""

    __slots__ = ("_data", "_media_type")

    def __init__(self, data: bytes, media_type: Optional[MediaType]) -> None:
        self._data = data
        self._media_type = media_type

    def media_type(self) -> Optional[MediaType]:
        return self._media_type

    def content_length(self) -> int:
        return len(self._data)

    def is_replayable(self) -> bool:
        return True

    def to_replayable(self, provider: Optional[IoProvider] = None) -> "RequestBody":
        return self

    def write_to(self, sink: BufferedSink) -> None:
        sink.write_bytes(self._data)


class _BufferBody(RequestBody):
    """Replayable body backed by an in-memory :class:`Buffer` (reads via peek)."""

    __slots__ = ("_buffer", "_media_type", "_length")

    def __init__(self, buffer: Buffer, media_type: Optional[MediaType], length: int) -> None:
        self._buffer = buffer
        self._media_type = media_type
        self._length = length

    def media_type(self) -> Optional[MediaType]:
        return self._media_type

    def content_length(self) -> int:
        return self._length

    def is_replayable(self) -> bool:
        return True

    def to_replayable(self, provider: Optional[IoProvider] = None) -> "RequestBody":
        return self

    def write_to(self, sink: BufferedSink) -> None:
        sink.write_all(self._buffer.peek())


class _SourceBody(RequestBody):
    """Single-use body backed by a :class:`BufferedSource`."""

    __slots__ = ("_source", "_media_type", "_length", "_consumed")

    def __init__(
        self,
        source: BufferedSource,
        media_type: Optional[MediaType],
        length: int,
    ) -> None:
        self._source = source
        self._media_type = media_type
        self._length = length
        self._consumed = False

    def media_type(self) -> Optional[MediaType]:
        return self._media_type

    def content_length(self) -> int:
        return self._length

    def write_to(self, sink: BufferedSink) -> None:
        if self._consumed:
            raise RuntimeError(
                "RequestBody.write_to was already called — the underlying source "
                "is exhausted. Call to_replayable() BEFORE write_to if retries "
                "may be needed."
            )
        self._consumed = True
        try:
            sink.write_all(self._source)
        finally:
            self._source.close()


__all__ = ["RequestBody"]
