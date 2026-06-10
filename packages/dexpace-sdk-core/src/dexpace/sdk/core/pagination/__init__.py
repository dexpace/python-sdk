# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Auto-pagination — drive a paged API through the pipeline.

The public surface is a small set of pieces that compose:

* `Page` — a frozen page of items plus the request that reaches the
  next page (and, when supported, the previous one). It is a context manager
  so the underlying response closes deterministically.
* `PaginationStrategy` — the SPI that turns one decoded response into a
  `Page`. Built-ins: `CursorStrategy` (cursor / token),
  `PageNumberStrategy` (page index), and `LinkHeaderStrategy`
  (RFC 5988 ``Link`` header).
* `Paginator` / `AsyncPaginator` — iterate the sequence
  item-by-item by default, or page-by-page via ``by_page``. Each page fetch
  runs through the full pipeline, so retry, auth, redirect, and tracing apply
  to every page.
* `parse_link_header` / `find_rel` — the standalone RFC 5988
  parser the link strategy is built on.
"""

from __future__ import annotations

from .link_header import ParsedLink, find_rel, parse_link_header
from .page import Page
from .paginator import (
    AsyncPaginator,
    AsyncPipelineLike,
    Paginator,
    SendAsync,
    SendSync,
    SyncPipelineLike,
)
from .strategy import (
    CursorStrategy,
    HasHeaders,
    LinkHeaderStrategy,
    PageNumberStrategy,
    PaginationStrategy,
)

__all__ = [
    "AsyncPaginator",
    "AsyncPipelineLike",
    "CursorStrategy",
    "HasHeaders",
    "LinkHeaderStrategy",
    "Page",
    "PageNumberStrategy",
    "PaginationStrategy",
    "Paginator",
    "ParsedLink",
    "SendAsync",
    "SendSync",
    "SyncPipelineLike",
    "find_rel",
    "parse_link_header",
]
