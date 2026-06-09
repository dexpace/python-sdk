# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of ``ResponseBody``."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable
from types import TracebackType
from typing import Self

from ..common.media_type import MediaType
from ..request.async_request_body import SupportsAsyncRead
from .response_body import _check_chunk_size

_bytes = bytes


async def _shielded_cleanup(cleanup: Awaitable[object]) -> None:
    """Run a cleanup coroutine without letting cancellation interrupt it.

    This is the single cancellation convention used by the async response
    bodies: a ``finally`` block that releases transport resources may run
    while an ``asyncio.CancelledError`` is already propagating through the
    enclosing task. Awaiting the cleanup directly would let that
    cancellation interrupt it mid-way, leaking the underlying connection.

    The cleanup is wrapped in ``asyncio.shield`` so it always runs to
    completion. If the surrounding scope is cancelled, the ``CancelledError``
    raised by ``shield`` is caught and the wait retried until the shielded
    cleanup finishes; the cancellation is then re-raised so it continues to
    propagate. Cleanup never swallows cancellation — it merely defers it
    until the resource is released. A ``CancelledError`` raised because the
    cleanup *itself* was cancelled is propagated immediately.

    Args:
        cleanup: The resource-release coroutine to run to completion.

    Raises:
        asyncio.CancelledError: Re-raised after the cleanup completes when
            the enclosing scope was cancelled while the cleanup ran.
    """
    inner = asyncio.ensure_future(cleanup)
    cancelled = False
    while True:
        try:
            await asyncio.shield(inner)
            break
        except asyncio.CancelledError:
            if inner.cancelled():
                # The cleanup itself was cancelled, not just our wait on it.
                raise
            # An outer cancellation hit our wait, not the shielded cleanup.
            # Keep waiting until the cleanup finishes, then re-raise so the
            # cancellation continues to propagate.
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


class AsyncResponseBody(ABC):
    """Async twin of ``ResponseBody``.

    Surfaces ``aiter_bytes`` / ``bytes()`` / ``string()`` and implements the
    async context-manager protocol so transport handles release
    deterministically.
    """

    @abstractmethod
    def media_type(self) -> MediaType | None:
        """Return the media type of the payload, or ``None`` if unknown."""

    @abstractmethod
    def content_length(self) -> int:
        """Return the number of bytes available, or ``-1`` if unknown."""

    @abstractmethod
    def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Yield the body's bytes in chunks; closes the body on exhaustion."""

    @abstractmethod
    async def close(self) -> None:
        """Release transport resources. Idempotent."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def bytes(self) -> _bytes:
        """Read the entire body as bytes and close the underlying stream."""
        chunks: list[bytes] = []
        try:
            async for chunk in self.aiter_bytes():
                chunks.append(chunk)
        finally:
            await _shielded_cleanup(self.close())
        return b"".join(chunks)

    async def string(self, encoding: str | None = None) -> str:
        """Read the entire body and decode it as text."""
        if encoding is None:
            media = self.media_type()
            encoding = (media.charset if media is not None else None) or "utf-8"
        raw = await self.bytes()
        return raw.decode(encoding)

    @classmethod
    def from_bytes(
        cls,
        data: _bytes,
        media_type: MediaType | None = None,
    ) -> AsyncResponseBody:
        return _AsyncBytesResponseBody(data, media_type)

    @classmethod
    def from_async_stream(
        cls,
        stream: SupportsAsyncRead,
        media_type: MediaType | None = None,
        content_length: int = -1,
    ) -> AsyncResponseBody:
        return _AsyncStreamResponseBody(stream, media_type, content_length)


class _AsyncBytesResponseBody(AsyncResponseBody):
    """In-memory ``AsyncResponseBody``."""

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

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        _check_chunk_size(chunk_size)
        if self._consumed:
            raise RuntimeError("AsyncResponseBody has already been consumed")
        self._consumed = True
        view = memoryview(self._data)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])
        await self.close()

    async def close(self) -> None:
        self._closed = True


class _AsyncStreamResponseBody(AsyncResponseBody):
    """Stream-backed single-use ``AsyncResponseBody``.

    Note:
        Cancellation contract. The generator returned by ``aiter_bytes``
        relies on a ``finally`` clause to release the underlying stream. If
        the consuming task is cancelled mid-iteration, that ``finally`` block
        runs while a ``CancelledError`` is already in flight. The cleanup is
        routed through ``_shielded_cleanup``, which wraps the inner
        ``await self._stream.close()`` in ``asyncio.shield`` so the close runs
        to completion before the ``CancelledError`` is re-raised. The transport
        handle is therefore released even when the iterating task is cancelled
        mid-stream, and the cancellation continues to propagate afterwards —
        cleanup never swallows it.
    """

    __slots__ = ("_closed", "_consumed", "_length", "_media_type", "_stream")

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
        self._closed = False

    def media_type(self) -> MediaType | None:
        return self._media_type

    def content_length(self) -> int:
        return self._length

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        _check_chunk_size(chunk_size)
        if self._consumed:
            raise RuntimeError("AsyncResponseBody has already been consumed")
        self._consumed = True
        try:
            while True:
                chunk = await self._stream.read(chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _shielded_cleanup(self._stream.close())


__all__ = ["AsyncResponseBody"]
