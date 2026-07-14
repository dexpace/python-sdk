# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Certify the production SSE parser against the golden byte corpus.

The ``sse`` family of the golden wire-exactness corpus pins the parsed events
for a set of raw byte streams that exercise the full WHATWG grammar across
``LF`` / ``CR`` / ``CRLF`` terminators, a leading UTF-8 BOM, multi-line data
concatenation, sticky ``id``, ``retry`` parsing, comments, the single-space
strip, UTF-8 payloads, and the trailing event with no final blank line.

Unlike ``tests/test_golden_corpus.py`` — which validates the fixtures against
small stdlib oracles — this suite drives the real ``SseParser`` (through
``parse_events`` / ``parse_async_events``) so the shipped parser is held to the
corpus byte-for-byte. Each vector is checked three ways:

- fed as one whole-stream chunk (sync),
- fed one byte at a time so every terminator and the BOM straddle a chunk
  boundary (sync), and
- fed through the async driver (whole stream).

All three must reproduce the corpus's expected event sequence exactly, proving
the parse is independent of transport chunking.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from dexpace.sdk.core.http.sse import SseEvent, parse_async_events, parse_events
from dexpace.sdk.tck.golden_corpus import SseExpectedEvent, SseVector, load_sse

_SSE_VECTORS: tuple[SseVector, ...] = load_sse()
_VECTOR_IDS: list[str] = [vector.id for vector in _SSE_VECTORS]


def _expected(events: tuple[SseExpectedEvent, ...]) -> list[SseEvent]:
    """Map corpus expectations onto the parser's ``SseEvent`` shape."""
    return [
        SseEvent(data=event.data, event=event.event, id=event.id, retry=event.retry)
        for event in events
    ]


@pytest.mark.parametrize("vector", _SSE_VECTORS, ids=_VECTOR_IDS)
def test_golden_corpus_whole_stream(vector: SseVector) -> None:
    """Parsing the whole stream reproduces the corpus events byte-for-byte."""
    events = list(parse_events([vector.stream]))
    assert events == _expected(vector.events)


@pytest.mark.parametrize("vector", _SSE_VECTORS, ids=_VECTOR_IDS)
def test_golden_corpus_byte_by_byte(vector: SseVector) -> None:
    """One-byte chunks — terminators and the BOM straddle every boundary."""
    chunks = [vector.stream[i : i + 1] for i in range(len(vector.stream))]
    events = list(parse_events(chunks))
    assert events == _expected(vector.events)


@pytest.mark.parametrize("vector", _SSE_VECTORS, ids=_VECTOR_IDS)
async def test_golden_corpus_async_stream(vector: SseVector) -> None:
    """The async driver reproduces the same corpus events."""

    async def producer() -> AsyncIterator[bytes]:
        yield vector.stream

    events = [event async for event in parse_async_events(producer())]
    assert events == _expected(vector.events)
