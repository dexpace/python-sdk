# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the synchronous ``Paginator`` driven through a mock pipeline."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.errors import DeserializationError
from dexpace.sdk.core.http.common import Headers, MediaType, Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, Status
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.pagination import AsyncPaginator, CursorStrategy, Paginator


class _MockPipeline:
    """Stand-in ``Pipeline``: maps a cursor query value to a canned page body.

    Records every request it is handed so tests can assert the paginator
    drove the pipeline (not a bare transport) and built the right next URL.
    """

    def __init__(self, pages: dict[str | None, dict[str, object]]) -> None:
        self._pages = pages
        self.calls: list[Request] = []
        self.closed_bodies: list[_TrackingBody] = []

    def run(self, request: Request, _dispatch: DispatchContext) -> Response:
        self.calls.append(request)
        cursor = request.url.query.get("cursor")
        payload = self._pages[cursor]
        body = _TrackingBody(json.dumps(payload).encode("utf-8"))
        self.closed_bodies.append(body)
        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.OK,
            headers=Headers(),
            body=body,
        )


class _TrackingBody(ResponseBody):
    """In-memory body that records when it is closed (for cleanup assertions)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield self._data

    def close(self) -> None:
        self.closed = True


def _first_request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://api.example.com/items"))


def _strategy() -> CursorStrategy[int]:
    return CursorStrategy(
        items_field="data",
        cursor_response_field="next_cursor",
        cursor_param="cursor",
    )


def _three_page_pipeline() -> _MockPipeline:
    return _MockPipeline(
        {
            None: {"data": [1, 2], "next_cursor": "c1"},
            "c1": {"data": [3, 4], "next_cursor": "c2"},
            "c2": {"data": [5], "next_cursor": None},
        },
    )


def test_iterates_items_across_all_pages() -> None:
    pipeline = _three_page_pipeline()
    paginator: Paginator[int] = Paginator(pipeline, _strategy(), _first_request())
    assert list(paginator) == [1, 2, 3, 4, 5]


def test_drives_the_pipeline_once_per_page() -> None:
    pipeline = _three_page_pipeline()
    paginator: Paginator[int] = Paginator(pipeline, _strategy(), _first_request())
    list(paginator)
    assert len(pipeline.calls) == 3
    assert pipeline.calls[1].url.query.get("cursor") == "c1"
    assert pipeline.calls[2].url.query.get("cursor") == "c2"


def test_by_page_yields_pages_with_raw_response() -> None:
    pipeline = _three_page_pipeline()
    paginator: Paginator[int] = Paginator(pipeline, _strategy(), _first_request())
    pages = list(paginator.by_page())
    assert [list(page.items) for page in pages] == [[1, 2], [3, 4], [5]]
    assert all(isinstance(page.raw, Response) for page in pages)


def test_item_iteration_closes_each_page_body() -> None:
    pipeline = _three_page_pipeline()
    paginator: Paginator[int] = Paginator(pipeline, _strategy(), _first_request())
    list(paginator)
    assert all(body.closed for body in pipeline.closed_bodies)


def test_max_pages_bounds_iteration() -> None:
    pipeline = _three_page_pipeline()
    paginator: Paginator[int] = Paginator(
        pipeline,
        _strategy(),
        _first_request(),
        max_pages=2,
    )
    assert list(paginator) == [1, 2, 3, 4]
    assert len(pipeline.calls) == 2


def test_accepts_a_plain_send_callable() -> None:
    pipeline = _three_page_pipeline()

    def send(request: Request) -> Response:
        return pipeline.run(request, DispatchContext.noop())

    paginator: Paginator[int] = Paginator(send, _strategy(), _first_request())
    assert list(paginator) == [1, 2, 3, 4, 5]


def test_single_page_sequence_terminates() -> None:
    pipeline = _MockPipeline({None: {"data": [7, 8], "next_cursor": None}})
    paginator: Paginator[int] = Paginator(pipeline, _strategy(), _first_request())
    assert list(paginator) == [7, 8]
    assert len(pipeline.calls) == 1


class _RawBodyPipeline:
    """Pipeline returning a fixed, possibly non-JSON, body for every request."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def run(self, request: Request, _dispatch: DispatchContext) -> Response:
        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=Status.OK,
            headers=Headers(),
            body=_TrackingBody(self._raw),
        )


def test_malformed_json_body_raises_deserialization_error() -> None:
    # An HTML error page served with a 200 by a load balancer must surface as
    # an SDK error, not a bare json.JSONDecodeError that escapes the hierarchy.
    pipeline = _RawBodyPipeline(b"<html><body>502 Bad Gateway</body></html>")
    paginator: Paginator[int] = Paginator(pipeline, _strategy(), _first_request())
    with pytest.raises(DeserializationError) as info:
        list(paginator)
    assert info.value.continuation_token == "https://api.example.com/items"


def test_async_pipeline_handed_to_sync_paginator_fails_fast() -> None:
    class _AsyncPipeline:
        async def run(self, request: Request, _dispatch: DispatchContext) -> Response:
            raise AssertionError("should never run")  # pragma: no cover

    with pytest.raises(TypeError, match="async pipeline"):
        Paginator(_AsyncPipeline(), _strategy(), _first_request())  # type: ignore[arg-type]


def test_async_send_callable_handed_to_sync_paginator_fails_fast() -> None:
    async def send(request: Request) -> Response:
        raise AssertionError("should never run")  # pragma: no cover

    with pytest.raises(TypeError, match="async send-callable"):
        Paginator(send, _strategy(), _first_request())  # type: ignore[arg-type]


def test_sync_pipeline_handed_to_async_paginator_fails_fast() -> None:
    pipeline = _three_page_pipeline()
    with pytest.raises(TypeError, match="sync pipeline"):
        AsyncPaginator(pipeline, _strategy(), _first_request())  # type: ignore[arg-type]


def test_sync_send_callable_handed_to_async_paginator_fails_fast() -> None:
    def send(request: Request) -> Response:
        raise AssertionError("should never run")  # pragma: no cover

    with pytest.raises(TypeError, match="sync send-callable"):
        AsyncPaginator(send, _strategy(), _first_request())  # type: ignore[arg-type]
