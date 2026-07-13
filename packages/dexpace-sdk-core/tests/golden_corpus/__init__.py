# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Golden wire-exactness byte corpus.

Checked-in raw byte fixtures — URL/query codec, redirect ``Location``
resolution, raw-query pagination splices, SSE streams, ``Retry-After`` pacing
vectors, and RFC 1123 dates — plus a small loader API. The corpus is the single
source of truth that keeps those behaviours byte-exact; later work points its
own conformance checks at these vectors.

The fixtures live under ``data/`` as raw byte files organised by family, read
in binary mode via ``importlib.resources`` so newline forms and percent-encoded
bytes are never mangled. The layout is self-contained so the corpus can later
be lifted into a standalone TCK distribution and shipped as package data.
"""

from __future__ import annotations

from .loader import (
    FAMILIES,
    FAMILY_TAGS,
    GoldenVector,
    PaginationSpliceVector,
    QueryCodecVector,
    RedirectVector,
    RetryPacingVector,
    Rfc1123Vector,
    SseExpectedEvent,
    SseVector,
    load_all,
    load_pagination_splice,
    load_query_codec,
    load_redirect,
    load_retry_pacing,
    load_rfc1123_dates,
    load_sse,
)

__all__ = [
    "FAMILIES",
    "FAMILY_TAGS",
    "GoldenVector",
    "PaginationSpliceVector",
    "QueryCodecVector",
    "RedirectVector",
    "RetryPacingVector",
    "Rfc1123Vector",
    "SseExpectedEvent",
    "SseVector",
    "load_all",
    "load_pagination_splice",
    "load_query_codec",
    "load_redirect",
    "load_retry_pacing",
    "load_rfc1123_dates",
    "load_sse",
]
