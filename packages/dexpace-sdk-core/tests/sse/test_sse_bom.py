# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for leading UTF-8 BOM handling in the WHATWG SSE parser (fix F4)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from dexpace.sdk.core.errors import StreamingError
from dexpace.sdk.core.http.sse import SseEvent, parse_async_events, parse_events
from dexpace.sdk.core.http.sse.parser import SseParser

_BOM = b"\xef\xbb\xbf"


def _events(stream: bytes, chunk_size: int = 4096) -> list[SseEvent]:
    return list(parse_events(_chunked(stream, chunk_size)))


def _chunked(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def test_leading_bom_stripped_so_first_field_parses() -> None:
    stream = _BOM + b"data: hello\n\n"
    assert _events(stream) == [SseEvent(data="hello")]


def test_leading_bom_strips_only_once() -> None:
    # Only the first BOM is stripped; a second is ordinary content that
    # corrupts the field name, so the event yields no data and is dropped.
    stream = _BOM + _BOM + b"data: hi\n\n"
    assert _events(stream) == []


def test_bom_split_across_chunks_is_still_stripped() -> None:
    stream = _BOM + b"data: split\n\n"
    # Force every byte (and thus the BOM) to arrive one at a time.
    assert _events(stream, chunk_size=1) == [SseEvent(data="split")]


def test_bom_split_two_then_rest() -> None:
    parser = SseParser()
    parser.feed(_BOM[:2])
    assert list(parser.drain()) == []
    parser.feed(_BOM[2:] + b"data: x\n\n")
    assert list(parser.drain()) == [SseEvent(data="x")]


def test_no_bom_first_field_unaffected() -> None:
    stream = b"data: plain\n\n"
    assert _events(stream) == [SseEvent(data="plain")]


def test_feff_not_at_start_is_not_stripped() -> None:
    # U+FEFF after the first event is content, never a BOM.
    stream = b"data: a\n\n" + "data: ﻿b".encode() + b"\n\n"
    events = _events(stream)
    assert events[0] == SseEvent(data="a")
    assert events[1] == SseEvent(data="﻿b")


def test_bom_only_stream_at_eos_emits_nothing() -> None:
    assert _events(_BOM) == []


def test_partial_bom_only_stream_at_eos_decodes_as_content() -> None:
    # A lone first BOM byte that never completes is not a BOM; it is a
    # truncated UTF-8 sequence and must surface as a mid-codepoint error.
    parser = SseParser()
    parser.feed(_BOM[:1])
    with pytest.raises(StreamingError):
        list(parser.end())


def test_bom_does_not_regress_max_line_cap() -> None:
    parser = SseParser(max_line_bytes=16)
    with pytest.raises(StreamingError):
        parser.feed(_BOM + b"data: " + b"x" * 64)


def test_bom_does_not_regress_mid_codepoint_guard() -> None:
    parser = SseParser()
    # Leading BOM, then a truncated multi-byte codepoint at end-of-stream.
    parser.feed(_BOM + b"data: \xe2\x82")  # start of U+20AC, cut short
    with pytest.raises(StreamingError):
        list(parser.end())


async def test_leading_bom_stripped_in_async_path() -> None:
    async def producer() -> AsyncIterator[bytes]:
        yield _BOM[:1]
        yield _BOM[1:]
        yield b"data: async\n\n"

    events: list[SseEvent] = []
    async for event in parse_async_events(producer()):
        events.append(event)
    assert events == [SseEvent(data="async")]
