# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Streaming helpers — JSONL parsing and HTTP/1.1 chunked-transfer framing.

These are SansIO utilities: they consume / produce ``Iterator[bytes]`` (or
``AsyncIterator[bytes]``) and do no I/O on their own.

Lifecycle discipline (mirroring the SSE parser). The decoders work on raw
``bytes`` throughout — the buffer never passes through a text-mode stream, so a
multi-byte codepoint split across a chunk boundary is reassembled from bytes and
line boundaries stay intact. When iteration ends — on exhaustion, on error, or
when the consumer abandons the iterator part-way — the underlying byte stream's
``close`` / ``aclose`` is invoked so an interrupted read never leaks the response
body. A close failure raised while another exception is already unwinding the
decoder is chained onto that exception's ``__context__`` rather than masking it;
the async release is routed through the shared cancellation-shielded cleanup so
the transport handle is freed even when the consuming task is cancelled.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from typing import Any

from ...errors.serialization import DeserializationError
from ...errors.streaming import StreamingError
from .._shielded import _shielded_cleanup


def _append_context(primary: BaseException, extra: BaseException) -> None:
    """Attach ``extra`` to the tail of ``primary``'s ``__context__`` chain.

    Used when a teardown-time close fails while another exception is already
    propagating: the primary error must keep propagating (it is not masked),
    but the close failure is preserved for diagnostics rather than swallowed.
    Any implicit context Python set on ``extra`` back to ``primary`` while the
    teardown ran is cleared first, and the walk is cycle-guarded, so no
    reference cycle is created.

    Args:
        primary: The exception that must continue to propagate.
        extra: The teardown-time close error to preserve on the chain.
    """
    extra.__context__ = None
    node = primary
    seen = {id(primary), id(extra)}
    while node.__context__ is not None and id(node.__context__) not in seen:
        node = node.__context__
        seen.add(id(node))
    if node.__context__ is None:
        node.__context__ = extra


def _close_upstream(iterator: object, primary: BaseException | None) -> None:
    """Release a sync upstream iterator during decoder teardown.

    Calls the iterator's ``close`` if it exposes one (a bare ``iter`` result
    without ``close`` is left untouched). A close failure surfaces when the
    decoder finished cleanly, but is chained onto ``primary`` — never allowed
    to mask it — when an exception is already unwinding the decoder.

    Args:
        iterator: The upstream byte iterator to release.
        primary: The exception already propagating out of the decoder, or
            ``None`` on a clean finish.

    Raises:
        Exception: The close failure, only when ``primary`` is ``None``.
    """
    close = getattr(iterator, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception as close_err:
        if primary is None:
            raise
        _append_context(primary, close_err)


async def _aclose_upstream(iterator: object, primary: BaseException | None) -> None:
    """Async twin of `_close_upstream`.

    Routes the upstream ``aclose`` through the shared shielded-cleanup
    convention so the transport handle is released even when the consuming task
    is cancelled mid-stream, mirroring the SSE async stream.

    Args:
        iterator: The upstream async byte iterator to release.
        primary: The exception already propagating out of the decoder, or
            ``None`` on a clean finish.

    Raises:
        Exception: The close failure, only when ``primary`` is ``None``.
    """
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await _shielded_cleanup(aclose())
    except Exception as close_err:
        if primary is None:
            raise
        _append_context(primary, close_err)


def iter_jsonl(chunks: Iterable[bytes]) -> Iterator[Any]:
    """Parse a newline-delimited JSON stream.

    Each line is decoded as UTF-8 and JSON-parsed. Empty lines are skipped.
    Buffering handles chunk-boundary splits. The underlying byte stream is
    released when iteration ends — on exhaustion, on error, or when the
    consumer abandons the iterator part-way — so an interrupted read never
    leaks the response body.

    Args:
        chunks: Iterable of byte chunks (typically ``response.body.iter_bytes()``).

    Yields:
        One parsed Python value per non-empty line.

    Raises:
        DeserializationError: If a line is not valid JSON.
        StreamingError: If a line is not valid UTF-8 (e.g. a non-UTF-8 byte
            sequence or a codepoint truncated by a short final line).
    """
    iterator = iter(chunks)
    buffer = bytearray()
    try:
        for chunk in iterator:
            buffer.extend(chunk)
            yield from _drain_lines(buffer)
        if buffer:
            yield from _drain_lines(buffer, final=True)
    finally:
        _close_upstream(iterator, sys.exc_info()[1])


async def aiter_jsonl(chunks: AsyncIterable[bytes]) -> AsyncIterator[Any]:
    """Async twin of ``iter_jsonl``."""
    iterator = aiter(chunks)
    buffer = bytearray()
    try:
        async for chunk in iterator:
            buffer.extend(chunk)
            for value in _drain_lines(buffer):
                yield value
        if buffer:
            for value in _drain_lines(buffer, final=True):
                yield value
    finally:
        await _aclose_upstream(iterator, sys.exc_info()[1])


def _drain_lines(buffer: bytearray, *, final: bool = False) -> Iterator[Any]:
    while True:
        nl = buffer.find(b"\n")
        if nl < 0:
            if final and buffer:
                tail = bytes(buffer)
                buffer.clear()
                yield from _parse_line(tail)
            return
        line = bytes(buffer[:nl])
        del buffer[: nl + 1]
        yield from _parse_line(line)


def _parse_line(line: bytes) -> Iterator[Any]:
    try:
        text = line.rstrip(b"\r").decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as err:
        raise StreamingError("JSONL line is not valid UTF-8") from err
    if not text:
        return
    try:
        yield json.loads(text)
    except json.JSONDecodeError as err:
        raise DeserializationError(str(err), error=err) from err


def chunked_frame(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Wrap a byte stream in HTTP/1.1 chunked-transfer framing.

    Each input chunk is emitted as ``{hex-size}\\r\\n{chunk}\\r\\n``. After the
    input is exhausted, a trailing ``0\\r\\n\\r\\n`` terminator is emitted. The
    source byte stream is released when framing ends — on exhaustion, on error,
    or when the consumer abandons the iterator part-way.

    Args:
        chunks: Iterable of byte chunks to wrap.

    Yields:
        Framed bytes ready for HTTP/1.1 chunked transfer.
    """
    iterator = iter(chunks)
    try:
        for chunk in iterator:
            if not chunk:
                continue
            yield f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n"
        yield b"0\r\n\r\n"
    finally:
        _close_upstream(iterator, sys.exc_info()[1])


async def aiter_chunked_frame(chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """Async twin of ``chunked_frame``."""
    iterator = aiter(chunks)
    try:
        async for chunk in iterator:
            if not chunk:
                continue
            yield f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n"
        yield b"0\r\n\r\n"
    finally:
        await _aclose_upstream(iterator, sys.exc_info()[1])


__all__ = ["aiter_chunked_frame", "aiter_jsonl", "chunked_frame", "iter_jsonl"]
