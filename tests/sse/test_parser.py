"""Tests for the WHATWG SSE parser."""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest

from dexpace.sdk.core.errors import StreamingError
from dexpace.sdk.core.http.sse import SseEvent, parse_events
from dexpace.sdk.core.http.sse.parser import SseParser


def _events(stream: bytes, chunk_size: int = 4096) -> list[SseEvent]:
    return list(parse_events(_chunked(stream, chunk_size)))


def _chunked(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def test_single_event() -> None:
    stream = b"data: hello\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="hello")]


def test_multi_line_data_joined_with_newline() -> None:
    stream = b"data: line1\ndata: line2\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="line1\nline2")]


def test_event_field() -> None:
    stream = b"event: update\ndata: x\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="x", event="update")]


def test_id_persists_across_events() -> None:
    stream = b"id: 1\ndata: a\n\ndata: b\n\n"
    events = _events(stream)
    assert events[0].id == "1"
    assert events[1].id == "1"


def test_retry_parsed() -> None:
    stream = b"retry: 5000\ndata: x\n\n"
    events = _events(stream)
    assert events[0].retry == 5000


def test_retry_ignored_when_non_numeric() -> None:
    stream = b"retry: soon\ndata: x\n\n"
    events = _events(stream)
    assert events[0].retry is None


def test_comment_lines_ignored() -> None:
    stream = b": keepalive\ndata: x\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="x")]


def test_crlf_line_terminators() -> None:
    stream = b"data: x\r\n\r\n"
    events = _events(stream)
    assert events == [SseEvent(data="x")]


def test_field_without_colon_treated_as_field_with_empty_value() -> None:
    stream = b"data\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="")]


def test_chunk_boundary_split_mid_line() -> None:
    stream = b"data: hello\n\n"
    # Force chunking that splits between every character.
    events = _events(stream, chunk_size=1)
    assert events == [SseEvent(data="hello")]


def test_unknown_field_skipped() -> None:
    stream = b"x-custom: thing\ndata: y\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="y")]


def test_final_event_without_trailing_blank() -> None:
    stream = b"data: tail"
    events = _events(stream)
    assert events == [SseEvent(data="tail")]


def test_oversized_line_raises() -> None:
    parser = SseParser(max_line_bytes=64)
    with pytest.raises(StreamingError):
        parser.feed(b"data: " + b"x" * 100)


def test_partial_utf8_at_eof_does_not_crash() -> None:
    # ä is 2 bytes 0xc3 0xa4; split between feed and end
    parser = SseParser()
    parser.feed(b"data: \xc3")
    # Should not raise UnicodeDecodeError; StreamingError is acceptable.
    with contextlib.suppress(StreamingError):
        list(parser.end())


async def test_async_parser() -> None:
    from dexpace.sdk.core.http.sse import parse_async_events

    async def producer() -> AsyncIterator[bytes]:
        yield b"data: a\n"
        yield b"\n"
        yield b"data: b\n\n"

    events: list[SseEvent] = []
    async for event in parse_async_events(producer()):
        events.append(event)
    assert events == [SseEvent(data="a"), SseEvent(data="b")]
