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


@pytest.mark.req("SSE-16")
def test_id_is_per_block_not_carried_forward() -> None:
    # The parser is single-pass and MUST NOT persist a last-event-id: each event
    # surfaces only the id present in its own block. The second block sent no
    # id, so it is absent (None) rather than the sticky "1".
    stream = b"id: 1\ndata: a\n\ndata: b\n\n"
    events = _events(stream)
    assert events[0].id == "1"
    assert events[1].id is None


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


@pytest.mark.req("SSE-6")
def test_comment_text_is_captured_alongside_data() -> None:
    # A ':'-led line is a comment: its text (after the single-space strip) is
    # captured on the event rather than discarded.
    stream = b": keepalive\ndata: x\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="x", comment="keepalive")]


@pytest.mark.req("SSE-6", "SSE-13")
def test_comment_only_block_dispatches() -> None:
    # A comment counts as a "field seen", so a comment-only keep-alive block is
    # still dispatched as a (non-empty) event with empty data.
    stream = b": keep-alive\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="", comment="keep-alive")]


@pytest.mark.req("SSE-6")
def test_comment_is_latest_wins_within_a_block() -> None:
    stream = b": first\n: second\ndata: x\n\n"
    events = _events(stream)
    assert events == [SseEvent(data="x", comment="second")]


@pytest.mark.req("SSE-10")
def test_event_name_absent_is_none_not_message() -> None:
    # An omitted event: field surfaces as None, never defaulted to "message".
    stream = b"data: x\n\n"
    assert _events(stream) == [SseEvent(data="x", event=None)]


@pytest.mark.req("SSE-10")
def test_event_name_stored_raw_latest_wins() -> None:
    stream = b"event: first\nevent: second\ndata: x\n\n"
    assert _events(stream) == [SseEvent(data="x", event="second")]


@pytest.mark.req("SSE-4")
def test_present_but_empty_event_is_recorded_and_dispatches() -> None:
    # An 'event:' with nothing after it is a present-but-empty field: recorded
    # as "" (distinct from absent None) and it counts as a field seen, so the
    # block dispatches even without data.
    stream = b"event:\n\n"
    assert _events(stream) == [SseEvent(data="", event="")]


@pytest.mark.req("SSE-4", "SSE-13")
def test_present_but_empty_id_dispatches_id_only_block() -> None:
    stream = b"id:\n\n"
    assert _events(stream) == [SseEvent(data="", id="")]


@pytest.mark.req("SSE-13")
def test_id_only_block_dispatches() -> None:
    stream = b"id: 5\n\n"
    assert _events(stream) == [SseEvent(data="", id="5")]


@pytest.mark.req("SSE-13")
def test_retry_only_block_dispatches() -> None:
    stream = b"retry: 4000\n\n"
    assert _events(stream) == [SseEvent(data="", retry=4000)]


@pytest.mark.req("SSE-13")
def test_pure_blank_block_emits_nothing() -> None:
    # A block in which no tracked field was set (only blank lines) is skipped.
    stream = b"\n\n\ndata: x\n\n"
    assert _events(stream) == [SseEvent(data="x")]


@pytest.mark.req("SSE-13")
def test_unknown_field_only_block_emits_nothing() -> None:
    # An unknown field is not tracked, so an unknown-field-only block dispatches
    # nothing (distinct from a comment-only block, which does).
    stream = b"x-custom: thing\n\ndata: y\n\n"
    assert _events(stream) == [SseEvent(data="y")]


@pytest.mark.req("SSE-9")
def test_id_containing_nul_is_ignored_entirely() -> None:
    # An id whose value contains U+0000 is ignored: it does not set the id and
    # does not, by itself, count as a field seen.
    stream = b"id: a\x00b\ndata: x\n\n"
    assert _events(stream) == [SseEvent(data="x", id=None)]


@pytest.mark.req("SSE-9")
def test_nul_id_does_not_overwrite_valid_id_in_same_block() -> None:
    stream = b"id: good\nid: a\x00b\ndata: x\n\n"
    assert _events(stream) == [SseEvent(data="x", id="good")]


@pytest.mark.req("SSE-9", "SSE-13")
def test_nul_id_only_block_emits_nothing() -> None:
    # A NUL id is not a field seen, so an id-with-NUL-only block dispatches
    # nothing rather than an id-less event.
    stream = b"id: a\x00b\n\ndata: y\n\n"
    assert _events(stream) == [SseEvent(data="y")]


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
