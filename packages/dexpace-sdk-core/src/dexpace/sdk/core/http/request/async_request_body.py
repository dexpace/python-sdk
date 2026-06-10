# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of ``RequestBody``."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from .._shielded import _shielded_cleanup
from ..common import common_media_types
from ..common.media_type import MediaType
from .request_body import _check_chunk_size


@runtime_checkable
class SupportsAsyncWrite(Protocol):
    """Async stream sink — anything with ``async def write(bytes) -> int | None``."""

    async def write(self, data: bytes) -> object: ...


@runtime_checkable
class SupportsAsyncRead(Protocol):
    """Async stream source — anything with ``async def read(size) -> bytes``."""

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> object: ...


class AsyncRequestBody(ABC):
    """Async twin of ``RequestBody``.

    Produces bytes via ``aiter_bytes``; ``write_to`` is the convenience
    drainer for transports holding a ``SupportsAsyncWrite``. This async
    surface ships a subset of the sync factories: ``from_bytes`` /
    ``from_string`` / ``from_form`` produce replayable bodies, while
    ``from_async_iter`` and ``from_async_stream`` are single-use. The sync
    ``from_file`` / ``from_multipart`` factories have no async twin here.
    """

    @abstractmethod
    def media_type(self) -> MediaType | None:
        """Media type of the payload, or ``None`` when unspecified."""

    def content_length(self) -> int:
        """Number of bytes ``aiter_bytes`` will produce, or ``-1`` if unknown."""
        return -1

    @abstractmethod
    def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Yield the body's bytes in chunks."""

    async def write_to(
        self,
        stream: SupportsAsyncWrite,
        chunk_size: int = 64 * 1024,
    ) -> int:
        """Drain the body into ``stream``. Returns total bytes written."""
        total = 0
        async for chunk in self.aiter_bytes(chunk_size):
            await stream.write(chunk)
            total += len(chunk)
        return total

    def is_replayable(self) -> bool:
        """True when ``aiter_bytes`` can be called more than once."""
        return False

    async def to_replayable(self) -> AsyncRequestBody:
        """Return a replayable equivalent (buffering once if needed)."""
        if self.is_replayable():
            return self
        chunks: list[bytes] = []
        async for chunk in self.aiter_bytes():
            chunks.append(chunk)
        return _AsyncBytesBody(b"".join(chunks), self.media_type())

    # ----- Factories ------------------------------------------------------

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        media_type: MediaType | None = None,
    ) -> AsyncRequestBody:
        return _AsyncBytesBody(data, media_type)

    @classmethod
    def from_string(
        cls,
        content: str,
        media_type: MediaType | None = None,
        encoding: str = "utf-8",
    ) -> AsyncRequestBody:
        return _AsyncBytesBody(content.encode(encoding), media_type)

    @classmethod
    def from_form(
        cls,
        fields: Mapping[str, str],
        encoding: str = "utf-8",
    ) -> AsyncRequestBody:
        encoded = "&".join(
            f"{quote(k, safe='', encoding=encoding)}={quote(v, safe='', encoding=encoding)}"
            for k, v in fields.items()
        )
        return _AsyncBytesBody(
            encoded.encode(encoding),
            common_media_types.APPLICATION_FORM_URLENCODED,
        )

    @classmethod
    def from_async_iter(
        cls,
        chunks: AsyncIterable[bytes],
        media_type: MediaType | None = None,
        content_length: int = -1,
    ) -> AsyncRequestBody:
        return _AsyncIterBody(chunks, media_type, content_length)

    @classmethod
    def from_async_stream(
        cls,
        stream: SupportsAsyncRead,
        media_type: MediaType | None = None,
        content_length: int = -1,
    ) -> AsyncRequestBody:
        return _AsyncStreamBody(stream, media_type, content_length)


class _AsyncBytesBody(AsyncRequestBody):
    """Replayable in-memory body."""

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

    async def to_replayable(self) -> AsyncRequestBody:
        return self

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        _check_chunk_size(chunk_size)
        view = memoryview(self._data)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])


class _AsyncIterBody(AsyncRequestBody):
    """Single-use body backed by an ``AsyncIterable[bytes]``."""

    __slots__ = ("_chunks", "_consumed", "_length", "_media_type")

    def __init__(
        self,
        chunks: AsyncIterable[bytes],
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

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        _check_chunk_size(chunk_size)
        del chunk_size
        if self._consumed:
            raise RuntimeError(
                "AsyncRequestBody.aiter_bytes was already called — call "
                "to_replayable() before iter if you need retries."
            )
        self._consumed = True
        async for chunk in self._chunks:
            yield chunk


class _AsyncStreamBody(AsyncRequestBody):
    """Single-use body backed by an async-read stream.

    Note:
        Cancellation contract. ``aiter_bytes`` releases the underlying stream
        from a ``finally`` clause. If the producing task is cancelled
        mid-iteration that clause runs with a ``CancelledError`` already in
        flight, so the close is routed through ``_shielded_cleanup`` to run to
        completion before the cancellation continues to propagate — cleanup
        never swallows it.
    """

    __slots__ = ("_consumed", "_length", "_media_type", "_stream")

    def __init__(
        self,
        stream: SupportsAsyncRead,
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

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        _check_chunk_size(chunk_size)
        if self._consumed:
            raise RuntimeError(
                "AsyncRequestBody.aiter_bytes was already called — the stream is exhausted."
            )
        self._consumed = True
        try:
            while True:
                chunk = await self._stream.read(chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            await _shielded_cleanup(self._stream.close())


__all__ = ["AsyncRequestBody", "SupportsAsyncRead", "SupportsAsyncWrite"]
