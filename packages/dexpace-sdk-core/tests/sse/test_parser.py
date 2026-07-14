# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

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


@pytest.mark.req("SSE-1", "SSE-15", "SSE-21")
def test_single_event() -> None:
    stream = b"data: hello\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="hello")]


def test_multi_line_data_joined_with_newline() -> None:
    stream = b"data: line1\ndata: line2\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="line1\nline2")]


@pytest.mark.req("SSE-21", "SSE-3", "SSE-5")
def test_event_field() -> None:
    stream = b"event: update\ndata: x\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="x", event="update")]


def test_id_persists_across_events() -> None:
    stream = b"id: 1\ndata: a\n\ndata: b\n\n"
    events = _events(stream)
    assert events[0].id == "1"
    assert events[1].id == "1"


@pytest.mark.req("SSE-11")
def test_retry_parsed() -> None:
    stream = b"retry: 5000\ndata: x\n\n"
    events = _events(stream)
    assert events[0].retry == 5000


@pytest.mark.req("SSE-11")
def test_retry_ignored_when_non_numeric() -> None:
    stream = b"retry: soon\ndata: x\n\n"
    events = _events(stream)
    assert events[0].retry is None


def test_retry_at_signed_64bit_ceiling_is_accepted() -> None:
    ceiling = (1 << 63) - 1
    stream = b"retry: " + str(ceiling).encode() + b"\ndata: x\n\n"
    events = _events(stream)
    assert events[0].retry == ceiling


@pytest.mark.req("SSE-11")
def test_retry_above_signed_64bit_ceiling_is_rejected_cleanly() -> None:
    # A digits-only value beyond the 2**63-1 millisecond cap is ignored (the
    # sticky retry stays unset) rather than stored as an unbounded integer.
    stream = b"retry: " + str(1 << 63).encode() + b"\ndata: x\n\n"
    events = _events(stream)
    assert events[0].retry is None


def test_retry_unicode_superscript_digit_does_not_raise() -> None:
    # "²".isdigit() is True but int("²") raises; the parser must treat
    # non-ASCII digits as non-numeric instead of leaking ValueError out of feed.
    stream = "retry: ²\ndata: x\n\n".encode()
    events = _events(stream)
    assert events[0].retry is None


@pytest.mark.req("SSE-11")
def test_retry_non_ascii_decimal_digit_is_ignored() -> None:
    # Arabic-Indic four (U+0664) is a Unicode decimal digit that int() accepts,
    # but the WHATWG spec restricts retry to ASCII digits, so it is ignored.
    stream = "retry: ٤\ndata: x\n\n".encode()
    events = _events(stream)
    assert events[0].retry is None


def test_comment_lines_ignored() -> None:
    stream = b": keepalive\ndata: x\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="x")]


@pytest.mark.req("SSE-2")
def test_crlf_line_terminators() -> None:
    stream = b"data: x\r\n\r\n"
    events = _events(stream)
    assert events == [SseEvent(data="x")]


@pytest.mark.req("SSE-3")
def test_field_without_colon_treated_as_field_with_empty_value() -> None:
    stream = b"data\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="")]


def test_chunk_boundary_split_mid_line() -> None:
    stream = b"data: hello\n\n"
    # Force chunking that splits between every character.
    events = _events(stream, chunk_size=1)
    assert events == [SseEvent(data="hello")]


@pytest.mark.req("SSE-7")
def test_unknown_field_skipped() -> None:
    stream = b"x-custom: thing\ndata: y\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="y")]


@pytest.mark.req("SSE-14")
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


def test_invalid_utf8_in_complete_line_decodes_with_replacement() -> None:
    # Per the WHATWG text/event-stream spec, invalid UTF-8 inside a complete
    # (terminated) line is decoded with U+FFFD replacement rather than raising
    # out of SseParser.feed. 0xff is never valid UTF-8.
    stream = b"data: \xffbad\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="�bad")]


def test_invalid_utf8_in_complete_line_does_not_raise_from_feed() -> None:
    # Feeding a complete line containing invalid UTF-8 must not raise; the
    # replacement-char decoding keeps the parser running on the same stream.
    parser = SseParser()
    parser.feed(b"data: \xff\n\n")
    events = list(parser.drain())
    assert events == [SseEvent(data="�")]


@pytest.mark.req("SSE-1", "SSE-15")
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
