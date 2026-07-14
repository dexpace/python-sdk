# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Full sync certification battery for `Paginator` / `Page`.

The lifecycle guarantees a paged operation must uphold — laziness, one
exchange per page, no fetch past the terminal page, idempotent end probes,
restart-per-``iter``, a positive-only page cap, eager per-page close on the
flat item view, a single-use context-managed ``by_page`` view, and the two
distinct ``__context__``-chaining behaviours around a failing close — all live
on the `Paginator` / `Page` pair in ``core.pagination`` (the only pagination
machinery that owns a response and is therefore responsible for closing it).
The callable-based ``ItemPaged`` / ``Pager`` in ``http.common.pagination`` has
no response to close and is exercised separately in ``tests/http``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.http.common import Headers, MediaType, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, Status
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.pagination import CursorStrategy, Page, Paginator
from dexpace.sdk.core.pagination.strategy import HasHeaders


class _LedgerBody(ResponseBody):
    """Test body that records only lifecycle (`close`) invocations.

    Unlike production bodies, reading it does not close it. That isolates the
    close ledger to the closes driven by `Page.close` / `Response.close`, so a
    count of exactly one per page proves the page owns and performs its close
    exactly once — separate from the orthogonal read-time close.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closes = 0

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield self._data

    def bytes(self) -> bytes:
        return self._data

    def string(self, encoding: str | None = None) -> str:
        return self._data.decode(encoding or "utf-8")

    def close(self) -> None:
        self.closes += 1


class _ExplodingCloseBody(_LedgerBody):
    """A ledger body whose `close` records the call and then raises."""

    def __init__(self, data: bytes, error: BaseException) -> None:
        super().__init__(data)
        self._error = error

    def close(self) -> None:
        self.closes += 1
        raise self._error


class _Source:
    """Exchange-counting send-callable mapping a cursor to a canned page body.

    Records every request it is handed so tests can assert exactly how many
    exchanges the paginator drove, and retains each body so the close ledger is
    observable.
    """

    def __init__(self, pages: dict[str | None, dict[str, object]]) -> None:
        self._pages = pages
        self.calls: list[Request] = []
        self.bodies: list[_LedgerBody] = []

    def __call__(self, request: Request) -> Response:
        self.calls.append(request)
        cursor = request.url.query.get("cursor")
        body = _LedgerBody(json.dumps(self._pages[cursor]).encode("utf-8"))
        self.bodies.append(body)
        return _response(request, body)


class _BoomStrategy:
    """Strategy that always raises when parsing — drives the parse-failure path."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def parse(
        self,
        response: HasHeaders,
        payload: object,
        template_request: Request,
    ) -> Page[int]:
        raise self._error


def _response(request: Request, body: ResponseBody) -> Response:
    return Response(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=Status.OK,
        headers=Headers(),
        body=body,
    )


def _first_request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/items"))


def _strategy() -> CursorStrategy[int]:
    return CursorStrategy(
        items_field="data",
        cursor_response_field="next_cursor",
        cursor_param="cursor",
    )


def _three_pages() -> dict[str | None, dict[str, object]]:
    return {
        None: {"data": [1, 2], "next_cursor": "c1"},
        "c1": {"data": [3, 4], "next_cursor": "c2"},
        "c2": {"data": [5], "next_cursor": None},
    }


def _looping_pages() -> dict[str | None, dict[str, object]]:
    # A non-advancing server: the cursor never resolves to None, so the
    # sequence would run forever without a cap.
    return {
        None: {"data": [0], "next_cursor": "loop"},
        "loop": {"data": [0], "next_cursor": "loop"},
    }


def _finite_pages(n: int) -> dict[str | None, dict[str, object]]:
    pages: dict[str | None, dict[str, object]] = {}
    for i in range(n):
        key = None if i == 0 else f"c{i}"
        nxt: str | None = f"c{i + 1}" if i < n - 1 else None
        pages[key] = {"data": [i], "next_cursor": nxt}
    return pages


# --- laziness / exchange counting -----------------------------------------


@pytest.mark.req("PAGE-6")
def test_no_exchange_until_consumption_starts() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    iter(paginator)  # obtaining the iterator must not fetch
    assert source.calls == []


@pytest.mark.req("PAGE-6")
def test_by_page_does_not_fetch_until_iterated() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    paginator.by_page()  # building the page view must not fetch
    assert source.calls == []


@pytest.mark.req("PAGE-6", "PAGE-7")
def test_exactly_one_exchange_per_page_and_no_overrun() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    assert list(paginator) == [1, 2, 3, 4, 5]
    # Three pages consumed, three exchanges — nothing fetched past the
    # terminal (``next_cursor: None``) page.
    assert len(source.calls) == 3


@pytest.mark.req("PAGE-7")
def test_end_of_pagination_probe_is_idempotent() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    with paginator.by_page() as pages:
        assert [list(page.items) for page in pages] == [[1, 2], [3, 4], [5]]
        assert len(source.calls) == 3
        # Probing the exhausted view again fires no further exchange.
        with pytest.raises(StopIteration):
            next(pages)
        with pytest.raises(StopIteration):
            next(pages)
        assert len(source.calls) == 3


@pytest.mark.req("PAGE-8")
def test_each_iter_restarts_pagination_from_the_start() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    assert list(paginator) == [1, 2, 3, 4, 5]
    assert list(paginator) == [1, 2, 3, 4, 5]
    # Two independent full walks, each with its own exchanges.
    assert len(source.calls) == 6


# --- page metadata survives response close --------------------------------


@pytest.mark.req("PAGE-2")
def test_page_metadata_survives_after_response_close() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    with paginator.by_page() as pages:
        page = next(pages)
        page.close()
        # Items were captured, not lazily read off the now-closed response.
        assert list(page.items) == [1, 2]
        # Status / headers remain readable off the raw response after close.
        assert isinstance(page.raw, Response)
        assert page.raw.status is Status.OK
        assert page.raw.headers == Headers()


# --- close-once ledger over every abandon path ----------------------------


def test_flat_iteration_closes_each_response_exactly_once() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    list(paginator)
    assert [body.closes for body in source.bodies] == [1, 1, 1]


@pytest.mark.req("PAGE-11")
def test_flat_item_view_closes_page_before_yielding_its_items() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    it = iter(paginator)
    assert next(it) == 1
    # The first page's response is already closed by the time its first item
    # reaches the caller — items are detached from any live connection.
    assert source.bodies[0].closes == 1


@pytest.mark.req("PAGE-12")
def test_by_page_context_exit_closes_each_page_once() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    with paginator.by_page() as pages:
        for page in pages:
            with page:
                _ = list(page.items)
    assert [body.closes for body in source.bodies] == [1, 1, 1]


@pytest.mark.req("PAGE-12")
def test_by_page_early_break_releases_held_page_once() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    with paginator.by_page() as pages:
        for _page in pages:
            break  # hold the first page without closing it
    # Only the first page was fetched (laziness) and it is closed exactly once.
    assert len(source.bodies) == 1
    assert source.bodies[0].closes == 1


@pytest.mark.req("PAGE-3")
def test_by_page_exhaustion_closes_every_page_once() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    with paginator.by_page() as pages:
        for page in pages:
            page.close()
    assert [body.closes for body in source.bodies] == [1, 1, 1]


@pytest.mark.req("PAGE-3")
def test_fetcher_does_not_close_pages_it_yields() -> None:
    # by_page yields live pages; closing is the caller's / page's job, never
    # the fetcher's. Without a close from the caller the responses stay open.
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    pages = list(paginator.by_page())
    assert [body.closes for body in source.bodies] == [0, 0, 0]
    assert len(pages) == 3


# --- single-use, context-managed page view --------------------------------


@pytest.mark.req("PAGE-14")
def test_by_page_is_single_use() -> None:
    source = _Source(_three_pages())
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    paginator.by_page()
    with pytest.raises(RuntimeError, match="single-use"):
        paginator.by_page()


# --- pagination cap --------------------------------------------------------


@pytest.mark.req("PAGE-9")
@pytest.mark.parametrize("bad_cap", [0, -1, -100])
def test_non_positive_cap_raises_at_construction(bad_cap: int) -> None:
    source = _Source(_three_pages())
    with pytest.raises(ValueError, match="max_pages"):
        Paginator(source, _strategy(), _first_request(), max_pages=bad_cap)


@pytest.mark.req("PAGE-9")
def test_non_advancing_server_stops_at_exactly_the_cap() -> None:
    source = _Source(_looping_pages())
    paginator: Paginator[int] = Paginator(
        source,
        _strategy(),
        _first_request(),
        max_pages=4,
    )
    items = list(paginator)
    assert len(source.calls) == 4  # exactly the cap — not 3, not 5
    assert items == [0, 0, 0, 0]


@pytest.mark.req("PAGE-10")
def test_default_cap_is_effectively_unbounded() -> None:
    source = _Source(_finite_pages(50))
    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    assert list(paginator) == list(range(50))
    assert len(source.calls) == 50


# --- error / __context__ chaining -----------------------------------------


@pytest.mark.req("PAGE-13")
def test_parse_failure_closes_inline_with_parse_error_primary() -> None:
    parse_error = RuntimeError("parse boom")
    close_error = OSError("close boom")

    def source(request: Request) -> Response:
        return _response(request, _ExplodingCloseBody(b'{"data": []}', close_error))

    paginator: Paginator[int] = Paginator(
        source,
        _BoomStrategy(parse_error),
        _first_request(),
    )
    with pytest.raises(RuntimeError) as info:
        list(paginator)
    # Parse error is primary; the close-time error is suppressed onto its
    # __context__, never masking the parse failure.
    assert info.value is parse_error
    assert info.value.__context__ is close_error


@pytest.mark.req("PAGE-15")
def test_held_page_close_error_wraps_the_original_failure() -> None:
    close_error = OSError("close boom")
    body = json.dumps({"data": [1], "next_cursor": None}).encode()

    def source(request: Request) -> Response:
        return _response(request, _ExplodingCloseBody(body, close_error))

    paginator: Paginator[int] = Paginator(source, _strategy(), _first_request())
    original = RuntimeError("work boom")
    with pytest.raises(OSError) as info, paginator.by_page() as pages:
        for _page in pages:
            raise original  # fails while holding a fetched-but-unconsumed page
    # The close-time error surfaces (wrapped); the original failure that led to
    # holding the page is the secondary note on __context__.
    assert info.value is close_error
    assert info.value.__context__ is original


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
