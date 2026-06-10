# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for CRLF terminators split across a chunk boundary in the SSE parser.

A CR at the very end of one chunk followed by an LF at the start of the next
must be recognised as a single CRLF terminator, not two separate line endings.
Otherwise one multi-line event is split into several.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from dexpace.sdk.core.http.sse import SseEvent, parse_async_events, parse_events
from dexpace.sdk.core.http.sse.parser import SseParser


def test_crlf_split_across_chunk_boundary_yields_single_event() -> None:
    # CR ends the first chunk; LF begins the second. The two halves form one
    # CRLF terminator, so the two data lines join into a single event.
    chunks = [b"data: a\r", b"\ndata: b\r\n\r\n"]
    events = list(parse_events(chunks))
    assert events == [SseEvent(data="a\nb")]


def test_cr_held_until_next_byte_disambiguates_lone_cr() -> None:
    # A lone CR (next byte is not LF) is still a terminator once the next byte
    # arrives, so the two data lines join into one event.
    chunks = [b"data: a\r", b"data: b\r\n\r\n"]
    events = list(parse_events(chunks))
    assert events == [SseEvent(data="a\nb")]


def test_trailing_cr_at_eos_is_a_terminator_not_literal_carriage_return() -> None:
    # A CR that is the final byte of the whole stream terminates the line; it
    # must not leak into the decoded field value as a literal '\r'.
    parser = SseParser()
    parser.feed(b"data: tail\r")
    events = list(parser.end())
    assert events == [SseEvent(data="tail")]


def test_trailing_cr_at_eos_single_feed() -> None:
    # Same property exercised through the convenience driver.
    events = list(parse_events([b"data: tail\r"]))
    assert events == [SseEvent(data="tail")]


async def test_crlf_split_across_chunk_boundary_async() -> None:
    async def producer() -> AsyncIterator[bytes]:
        yield b"data: a\r"
        yield b"\ndata: b\r\n\r\n"

    events: list[SseEvent] = []
    async for event in parse_async_events(producer()):
        events.append(event)
    assert events == [SseEvent(data="a\nb")]
