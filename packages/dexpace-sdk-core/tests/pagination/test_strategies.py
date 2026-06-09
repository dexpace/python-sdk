# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the built-in pagination strategies."""

from __future__ import annotations

from dexpace.sdk.core.http.common import Headers, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, Status
from dexpace.sdk.core.pagination import (
    CursorStrategy,
    LinkHeaderStrategy,
    PageNumberStrategy,
)


def _request(url: str = "https://api.example.com/items") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


def _response(req: Request, *, headers: Headers | None = None) -> Response:
    return Response(
        request=req,
        protocol=Protocol.HTTP_1_1,
        status=Status.OK,
        headers=headers or Headers(),
    )


class TestCursorStrategy:
    def test_extracts_items_and_builds_next_with_cursor_param(self) -> None:
        strategy: CursorStrategy[int] = CursorStrategy(
            items_field="data",
            cursor_response_field="next_cursor",
            cursor_param="cursor",
        )
        req = _request()
        payload = {"data": [1, 2, 3], "next_cursor": "abc123"}
        page = strategy.parse(_response(req), payload, req)
        assert page.items == [1, 2, 3]
        assert page.next_request is not None
        assert page.next_request.url.query.get("cursor") == "abc123"

    def test_token_convention_uses_configured_field_names(self) -> None:
        strategy: CursorStrategy[int] = CursorStrategy(
            items_field="results",
            cursor_response_field="next_page_token",
            cursor_param="page_token",
        )
        req = _request()
        payload = {"results": [9], "next_page_token": "tok"}
        page = strategy.parse(_response(req), payload, req)
        assert page.next_request is not None
        assert page.next_request.url.query.get("page_token") == "tok"

    def test_absent_cursor_ends_sequence(self) -> None:
        strategy: CursorStrategy[int] = CursorStrategy(items_field="data")
        req = _request()
        page = strategy.parse(_response(req), {"data": [1]}, req)
        assert page.next_request is None
        assert not page.has_next

    def test_empty_cursor_string_ends_sequence(self) -> None:
        strategy: CursorStrategy[int] = CursorStrategy(items_field="data")
        req = _request()
        page = strategy.parse(_response(req), {"data": [], "next_cursor": ""}, req)
        assert page.next_request is None

    def test_nested_dotted_item_path(self) -> None:
        strategy: CursorStrategy[int] = CursorStrategy(items_field="result.items")
        req = _request()
        payload = {"result": {"items": [1, 2]}, "next_cursor": "c"}
        page = strategy.parse(_response(req), payload, req)
        assert page.items == [1, 2]


class TestPageNumberStrategy:
    def test_increments_page_param_when_full_page(self) -> None:
        strategy: PageNumberStrategy[int] = PageNumberStrategy(
            items_field="data",
            page_size=2,
        )
        req = _request("https://api.example.com/items?page=1")
        page = strategy.parse(_response(req), {"data": [1, 2]}, req)
        assert page.next_request is not None
        assert page.next_request.url.query.get("page") == "2"

    def test_short_page_ends_sequence(self) -> None:
        strategy: PageNumberStrategy[int] = PageNumberStrategy(
            items_field="data",
            page_size=2,
        )
        req = _request("https://api.example.com/items?page=1")
        page = strategy.parse(_response(req), {"data": [1]}, req)
        assert page.next_request is None

    def test_empty_page_ends_sequence(self) -> None:
        strategy: PageNumberStrategy[int] = PageNumberStrategy(items_field="data")
        req = _request("https://api.example.com/items?page=4")
        page = strategy.parse(_response(req), {"data": []}, req)
        assert page.next_request is None

    def test_total_pages_field_bounds_iteration(self) -> None:
        strategy: PageNumberStrategy[int] = PageNumberStrategy(
            items_field="data",
            total_pages_field="total_pages",
        )
        req = _request("https://api.example.com/items?page=3")
        last = strategy.parse(_response(req), {"data": [1], "total_pages": 3}, req)
        assert last.next_request is None
        req2 = _request("https://api.example.com/items?page=2")
        more = strategy.parse(_response(req2), {"data": [1], "total_pages": 3}, req2)
        assert more.next_request is not None
        assert more.next_request.url.query.get("page") == "3"

    def test_defaults_to_start_page_when_param_absent(self) -> None:
        strategy: PageNumberStrategy[int] = PageNumberStrategy(
            items_field="data",
            start_page=1,
            page_size=2,
        )
        req = _request()
        page = strategy.parse(_response(req), {"data": [1, 2]}, req)
        assert page.next_request is not None
        assert page.next_request.url.query.get("page") == "2"


class TestLinkHeaderStrategy:
    def test_follows_rel_next_target(self) -> None:
        strategy: LinkHeaderStrategy[int] = LinkHeaderStrategy(items_field="data")
        headers = Headers(
            [("Link", '<https://api.example.com/items?page=2>; rel="next"')],
        )
        req = _request()
        page = strategy.parse(_response(req, headers=headers), {"data": [1]}, req)
        assert page.next_request is not None
        assert str(page.next_request.url) == "https://api.example.com/items?page=2"

    def test_exposes_prev_request_when_present(self) -> None:
        strategy: LinkHeaderStrategy[int] = LinkHeaderStrategy(items_field="data")
        headers = Headers(
            [
                (
                    "Link",
                    '<https://api.example.com/items?page=3>; rel="next", '
                    '<https://api.example.com/items?page=1>; rel="prev"',
                ),
            ],
        )
        req = _request()
        page = strategy.parse(_response(req, headers=headers), {"data": [1]}, req)
        assert page.prev_request is not None
        assert str(page.prev_request.url) == "https://api.example.com/items?page=1"

    def test_no_link_header_ends_sequence(self) -> None:
        strategy: LinkHeaderStrategy[int] = LinkHeaderStrategy(items_field="data")
        req = _request()
        page = strategy.parse(_response(req), {"data": [1]}, req)
        assert page.next_request is None
        assert page.prev_request is None

    def test_next_request_preserves_method_and_headers(self) -> None:
        strategy: LinkHeaderStrategy[int] = LinkHeaderStrategy(items_field="data")
        headers = Headers([("Link", '<https://api.example.com/p2>; rel="next"')])
        req = Request(
            method=Method.GET,
            url=Url.parse("https://api.example.com/items"),
            headers=Headers([("Authorization", "Bearer t")]),
        )
        page = strategy.parse(_response(req, headers=headers), {"data": [1]}, req)
        assert page.next_request is not None
        assert page.next_request.method is Method.GET
        assert page.next_request.headers.get("authorization") == "Bearer t"
