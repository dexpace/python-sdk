# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Property-based tests pinning SSE parser chunk-boundary invariance.

Invariant covered:

- Re-splitting the same byte stream at arbitrary chunk boundaries yields
  identical parsed events. ``SseParser`` buffers across chunk splits and
  handles ``LF`` / ``CR`` / ``CRLF`` terminators and the leading UTF-8 BOM, so
  the emitted ``SseEvent`` sequence must not depend on where the transport cut
  the stream — including cuts inside a multi-byte codepoint, inside a CRLF
  pair, or inside the BOM.

The payload alphabet is biased toward SSE structure (field names, colons,
line terminators) and includes multi-byte codepoints so re-splits exercise the
UTF-8 buffering path. Every payload is built from a ``str`` and encoded as
UTF-8, so it is always valid UTF-8 and never triggers the mid-codepoint
end-of-stream error path — that keeps the suite deterministically green while
still stressing arbitrary re-splitting.

The example count and deadline are governed by the capped "ci" Hypothesis
profile registered in ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpace.sdk.core.http.sse import SseEvent, parse_events

# Field-name fragments, structural punctuation, all three line terminators, a
# BOM, and multi-byte codepoints (2-, 3-, and 4-byte) so re-splits can land
# mid-codepoint.
_SSE_ALPHABET = "dataeventidretry: ;\t\r\n0123456789﻿é☃😀"
_sse_text = st.text(alphabet=_SSE_ALPHABET, max_size=120)


@st.composite
def _payload_and_chunks(draw: st.DrawFn) -> list[bytes]:
    """Encode arbitrary SSE-ish text and split it at arbitrary byte boundaries."""
    payload = draw(_sse_text).encode("utf-8")
    chunks: list[bytes] = []
    index = 0
    while index < len(payload):
        size = draw(st.integers(min_value=1, max_value=len(payload) - index))
        chunks.append(payload[index : index + size])
        index += size
    return chunks


@pytest.mark.req("SSE-40")
@given(chunks=_payload_and_chunks())
def test_events_invariant_under_chunk_resplits(chunks: list[bytes]) -> None:
    """Chunk boundaries do not change the parsed SSE event sequence."""
    whole = b"".join(chunks)
    baseline: list[SseEvent] = list(parse_events([whole]))
    resplit: list[SseEvent] = list(parse_events(chunks))
    assert resplit == baseline
