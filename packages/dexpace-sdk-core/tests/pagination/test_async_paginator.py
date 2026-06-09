# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncPaginator`` driven through a mock async pipeline."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from dexpace.sdk.core.http.common import Headers, MediaType, Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Status
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.pagination import AsyncPaginator, CursorStrategy


class _MockAsyncPipeline:
    """Async stand-in pipeline mapping a cursor value to a canned page body."""

    def __init__(self, pages: dict[str | None, dict[str, object]]) -> None:
        self._pages = pages
        self.calls: list[Request] = []
        self.closed_bodies: list[_TrackingAsyncBody] = []

    async def run(self, request: Request, _dispatch: DispatchContext) -> AsyncResponse:
        self.calls.append(request)
        cursor = request.url.query.get("cursor")
        payload = self._pages[cursor]
        body = _TrackingAsyncBody(json.dumps(payload).encode("utf-8"))
        self.closed_bodies.append(body)
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.OK,
            headers=Headers(),
            body=body,
        )


class _TrackingAsyncBody(AsyncResponseBody):
    """In-memory async body that records when it is closed."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        yield self._data

    async def close(self) -> None:
        self.closed = True


def _first_request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/items"))


def _strategy() -> CursorStrategy[int]:
    return CursorStrategy(
        items_field="data",
        cursor_response_field="next_cursor",
        cursor_param="cursor",
    )


def _three_page_pipeline() -> _MockAsyncPipeline:
    return _MockAsyncPipeline(
        {
            None: {"data": [1, 2], "next_cursor": "c1"},
            "c1": {"data": [3, 4], "next_cursor": "c2"},
            "c2": {"data": [5], "next_cursor": None},
        },
    )


async def test_iterates_items_across_all_pages() -> None:
    pipeline = _three_page_pipeline()
    paginator: AsyncPaginator[int] = AsyncPaginator(pipeline, _strategy(), _first_request())
    items = [item async for item in paginator]
    assert items == [1, 2, 3, 4, 5]


async def test_drives_pipeline_once_per_page_with_cursor() -> None:
    pipeline = _three_page_pipeline()
    paginator: AsyncPaginator[int] = AsyncPaginator(pipeline, _strategy(), _first_request())
    _ = [item async for item in paginator]
    assert len(pipeline.calls) == 3
    assert pipeline.calls[1].url.query.get("cursor") == "c1"
    assert pipeline.calls[2].url.query.get("cursor") == "c2"


async def test_by_page_yields_pages() -> None:
    pipeline = _three_page_pipeline()
    paginator: AsyncPaginator[int] = AsyncPaginator(pipeline, _strategy(), _first_request())
    pages = [list(page.items) async for page in paginator.by_page()]
    assert pages == [[1, 2], [3, 4], [5]]


async def test_max_pages_bounds_iteration() -> None:
    pipeline = _three_page_pipeline()
    paginator: AsyncPaginator[int] = AsyncPaginator(
        pipeline,
        _strategy(),
        _first_request(),
        max_pages=2,
    )
    items = [item async for item in paginator]
    assert items == [1, 2, 3, 4]
    assert len(pipeline.calls) == 2


async def test_item_iteration_closes_each_page_body() -> None:
    pipeline = _three_page_pipeline()
    paginator: AsyncPaginator[int] = AsyncPaginator(pipeline, _strategy(), _first_request())
    _ = [item async for item in paginator]
    assert all(body.closed for body in pipeline.closed_bodies)


async def test_accepts_a_plain_async_send_callable() -> None:
    pipeline = _three_page_pipeline()

    async def send(request: Request) -> AsyncResponse:
        return await pipeline.run(request, DispatchContext.noop())

    paginator: AsyncPaginator[int] = AsyncPaginator(send, _strategy(), _first_request())
    items = [item async for item in paginator]
    assert items == [1, 2, 3, 4, 5]
