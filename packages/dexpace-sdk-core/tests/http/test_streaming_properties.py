# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Property-based tests pinning ``iter_jsonl`` chunk-boundary invariance.

Invariant covered:

- Re-splitting the same byte stream at arbitrary chunk boundaries feeds the
  JSONL decoder identical output: ``iter_jsonl`` buffers across chunk splits,
  so the parsed values must not depend on where the transport happened to cut
  the stream (including cuts in the middle of a multi-byte UTF-8 codepoint).

The example count and deadline are governed by the capped "ci" Hypothesis
profile registered in ``tests/conftest.py``.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from dexpace.sdk.core.http.common import iter_jsonl

# JSON values that survive ``dumps`` -> ``loads`` exactly: NaN / Infinity are
# excluded because they either break value equality (NaN) or are not standard
# JSON, neither of which is relevant to the chunk-splitting invariant. Integers
# are bounded to 64-bit magnitudes so encoding never trips CPython's
# integer-string-conversion digit cap, which is unrelated to the invariant.
_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**63), max_value=2**63)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(st.characters(codec="utf-8")),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(st.characters(codec="utf-8"), max_size=8), children, max_size=4)
    ),
    max_leaves=10,
)


@st.composite
def _payload_and_chunks(draw: st.DrawFn) -> tuple[list[bytes], list[Any]]:
    """Render JSON values as a JSONL byte payload split at arbitrary boundaries.

    Returns:
        A ``(chunks, values)`` pair: ``chunks`` re-split the encoded payload at
        arbitrary byte boundaries, and ``values`` are the source Python values
        the decoder must reproduce.
    """
    values = draw(st.lists(_json_values, max_size=6))
    # ``ensure_ascii=False`` keeps multi-byte codepoints as UTF-8 bytes so the
    # re-split can land inside a codepoint; ``dumps`` never emits a literal
    # newline, so every value stays on its own line.
    payload = "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values).encode(
        "utf-8"
    )
    chunks: list[bytes] = []
    index = 0
    while index < len(payload):
        size = draw(st.integers(min_value=1, max_value=len(payload) - index))
        chunks.append(payload[index : index + size])
        index += size
    return chunks, values


@given(payload_and_chunks=_payload_and_chunks())
def test_iter_jsonl_invariant_under_chunk_resplits(
    payload_and_chunks: tuple[list[bytes], list[Any]],
) -> None:
    """Chunk boundaries do not change the decoded JSONL values."""
    chunks, values = payload_and_chunks
    whole = b"".join(chunks)
    baseline = list(iter_jsonl([whole]))
    resplit = list(iter_jsonl(chunks))
    assert resplit == baseline
    assert baseline == values
