# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pagination strategies — pure response-to-``Page`` translators.

A strategy is the only place that understands a particular API's pagination
convention. It is deliberately I/O-free: the paginator performs the request,
decodes the body into a plain Python value, and hands the strategy that value
together with the response (for header inspection) and the template request
(to derive the next page's request from). Keeping strategies pure lets the
sync and async paginators share them verbatim.

Three built-ins cover the common conventions:

* ``CursorStrategy`` — read an opaque cursor / continuation token out of the
  response body and resend it as a query parameter. One strategy covers both
  "cursor" and "token" pagination; they differ only in field names.
* ``PageNumberStrategy`` — increment a page-index query parameter until the
  server reports no more items (or an optional total-pages field is reached).
* ``LinkHeaderStrategy`` — follow the RFC 5988 ``Link`` header's ``rel="next"``
  target.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from urllib.parse import urljoin

from ..http.common.url import Url
from .link_header import find_rel
from .page import Page

if TYPE_CHECKING:
    from ..http.common.headers import Headers
    from ..http.request.request import Request


@runtime_checkable
class HasHeaders(Protocol):
    """Structural view of a response: just the header surface a strategy reads."""

    @property
    def headers(self) -> Headers: ...


@runtime_checkable
class PaginationStrategy[T](Protocol):
    """Translates one decoded response into a `Page`.

    Implementations are pure functions of their inputs — they perform no I/O
    and hold no mutable per-iteration state, so a single strategy instance is
    safe to reuse across both the sync and async paginators.
    """

    def parse(
        self,
        response: HasHeaders,
        payload: object,
        template_request: Request,
    ) -> Page[T]:
        """Build the page that ``response`` represents.

        Args:
            response: The response object, used for header inspection (e.g.
                the ``Link`` header). Its body must already be decoded into
                ``payload`` by the caller.
            payload: The decoded response body (typically a ``dict`` or
                ``list`` from JSON). ``None`` when the body was empty.
            template_request: The request that produced ``response``; the
                next page's request is derived from it so auth headers and
                path survive.

        Returns:
            A page whose ``next_request`` is ``None`` when the sequence is
            exhausted.
        """
        ...


def _dig(payload: object, path: Sequence[str]) -> object:
    """Walk a dotted key path into nested mappings; ``None`` if any step misses."""
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _items_at[T](payload: object, path: Sequence[str]) -> list[T]:
    found = _dig(payload, path)
    if isinstance(found, list):
        return cast("list[T]", found)
    return []


def _with_query_param(request: Request, name: str, value: str) -> Request:
    """Return ``request`` with query parameter ``name`` set to ``value``."""
    url = request.url
    return request.with_url(url.with_query(url.query.with_set(name, value)))


@dataclass(frozen=True, slots=True)
class CursorStrategy[T]:
    """Cursor / continuation-token pagination.

    Reads a cursor value out of the response body and resends it as a query
    parameter on the next request. Covers both the "cursor" convention
    (opaque string under e.g. ``next_cursor``) and the "token" convention
    (``next_page_token``) — they differ only in field names, supplied here.

    Args:
        items_field: Dotted path to the item list in the body (e.g.
            ``"data"`` or ``"result.items"``).
        cursor_response_field: Dotted path to the cursor in the body. An
            absent, empty, or ``null`` value ends the sequence.
        cursor_param: Query-parameter name to carry the cursor on the next
            request.
    """

    items_field: str = "items"
    cursor_response_field: str = "next_cursor"
    cursor_param: str = "cursor"

    def parse(
        self,
        response: HasHeaders,
        payload: object,
        template_request: Request,
    ) -> Page[T]:
        items: list[T] = _items_at(payload, self.items_field.split("."))
        cursor = _dig(payload, self.cursor_response_field.split("."))
        next_request: Request | None = None
        if isinstance(cursor, str) and cursor:
            next_request = _with_query_param(template_request, self.cursor_param, cursor)
        return Page(items=items, next_request=next_request, raw=response)


@dataclass(frozen=True, slots=True)
class PageNumberStrategy[T]:
    """Page-index pagination.

    Increments a numeric ``page`` query parameter each round. Termination is
    determined by the body: when the current page yields fewer items than
    ``page_size`` (when known) or an empty list, there is no next page. When
    ``total_pages_field`` is given, it is honoured as an explicit bound.

    Args:
        items_field: Dotted path to the item list in the body.
        page_param: Query-parameter name carrying the 1-based page index.
        start_page: The index of the first page (default ``1``).
        page_size: Expected items per full page; a short page signals the
            end. ``None`` disables the short-page heuristic.
        total_pages_field: Optional dotted path to a total-page count in the
            body; when present it bounds iteration explicitly.
    """

    items_field: str = "items"
    page_param: str = "page"
    start_page: int = 1
    page_size: int | None = None
    total_pages_field: str | None = None

    def parse(
        self,
        response: HasHeaders,
        payload: object,
        template_request: Request,
    ) -> Page[T]:
        items: list[T] = _items_at(payload, self.items_field.split("."))
        current = self._current_page(template_request)
        if self._is_last_page(items, payload, current):
            return Page(items=items, next_request=None, raw=response)
        next_request = _with_query_param(template_request, self.page_param, str(current + 1))
        return Page(items=items, next_request=next_request, raw=response)

    def _current_page(self, request: Request) -> int:
        raw = request.url.query.get(self.page_param)
        if raw is None:
            return self.start_page
        try:
            return int(raw)
        except ValueError:
            return self.start_page

    def _is_last_page(
        self,
        items: Sequence[object],
        payload: object,
        current: int,
    ) -> bool:
        if not items:
            return True
        if self.total_pages_field is not None:
            total = _dig(payload, self.total_pages_field.split("."))
            if isinstance(total, int):
                return current >= total
        if self.page_size is not None:
            return len(items) < self.page_size
        return False


@dataclass(frozen=True, slots=True)
class LinkHeaderStrategy[T]:
    """RFC 5988 ``Link``-header pagination.

    Follows the ``rel="next"`` target in the response's ``Link`` header and,
    when present, exposes the ``rel="prev"`` target as the page's previous
    request. The next request reuses the template request's method, headers,
    and body, swapping only the URL. A relative target (permitted by
    RFC 5988) is resolved against the template request's URL, so an API that
    returns ``</items?page=2>`` rather than an absolute URI still paginates.

    Args:
        items_field: Dotted path to the item list in the body.
        link_header_name: Header to read link relations from (default
            ``"Link"``).
    """

    items_field: str = "items"
    link_header_name: str = "Link"

    def parse(
        self,
        response: HasHeaders,
        payload: object,
        template_request: Request,
    ) -> Page[T]:
        items: list[T] = _items_at(payload, self.items_field.split("."))
        header = response.headers.get(self.link_header_name) or ""
        next_request = self._request_for(header, "next", template_request)
        prev_request = self._request_for(header, "prev", template_request)
        return Page(
            items=items,
            next_request=next_request,
            prev_request=prev_request,
            raw=response,
        )

    @staticmethod
    def _request_for(header: str, rel: str, template: Request) -> Request | None:
        target = find_rel(header, rel)
        if target is None:
            return None
        absolute = urljoin(str(template.url), target)
        return template.with_url(Url.parse(absolute))


if TYPE_CHECKING:
    # Static structural-conformance checks: each built-in must satisfy the
    # ``PaginationStrategy`` Protocol. Inheriting the Protocol would defeat
    # ``slots=True`` (it pulls in ``__dict__``), so we verify by assignment.
    _cursor_conforms: PaginationStrategy[object] = CursorStrategy()
    _page_conforms: PaginationStrategy[object] = PageNumberStrategy()
    _link_conforms: PaginationStrategy[object] = LinkHeaderStrategy()


__all__ = [
    "CursorStrategy",
    "HasHeaders",
    "LinkHeaderStrategy",
    "PageNumberStrategy",
    "PaginationStrategy",
]
