# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Streaming helpers — JSONL parsing and HTTP/1.1 chunked-transfer framing.

These are SansIO utilities: they consume / produce ``Iterator[bytes]`` (or
``AsyncIterator[bytes]``) and do no I/O on their own.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from typing import Any

from ...errors.serialization import DeserializationError
from ...errors.streaming import StreamingError


def iter_jsonl(chunks: Iterable[bytes]) -> Iterator[Any]:
    """Parse a newline-delimited JSON stream.

    Each line is decoded as UTF-8 and JSON-parsed. Empty lines are skipped.
    Buffering handles chunk-boundary splits.

    Args:
        chunks: Iterable of byte chunks (typically ``response.body.iter_bytes()``).

    Yields:
        One parsed Python value per non-empty line.

    Raises:
        DeserializationError: If a line is not valid JSON.
        StreamingError: If a line is not valid UTF-8 (e.g. a non-UTF-8 byte
            sequence or a codepoint truncated by a short final line).
    """
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        yield from _drain_lines(buffer)
    if buffer:
        yield from _drain_lines(buffer, final=True)


async def aiter_jsonl(chunks: AsyncIterable[bytes]) -> AsyncIterator[Any]:
    """Async twin of ``iter_jsonl``."""
    buffer = bytearray()
    async for chunk in chunks:
        buffer.extend(chunk)
        for value in _drain_lines(buffer):
            yield value
    if buffer:
        for value in _drain_lines(buffer, final=True):
            yield value


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
    input is exhausted, a trailing ``0\\r\\n\\r\\n`` terminator is emitted.

    Args:
        chunks: Iterable of byte chunks to wrap.

    Yields:
        Framed bytes ready for HTTP/1.1 chunked transfer.
    """
    for chunk in chunks:
        if not chunk:
            continue
        yield f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n"
    yield b"0\r\n\r\n"


async def aiter_chunked_frame(chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """Async twin of ``chunked_frame``."""
    async for chunk in chunks:
        if not chunk:
            continue
        yield f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n"
    yield b"0\r\n\r\n"


__all__ = ["aiter_chunked_frame", "aiter_jsonl", "chunked_frame", "iter_jsonl"]
