# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Full async certification battery for `AsyncPaginator` / `Page`.

The async twin of ``test_paginator_battery.py``. It certifies the lifecycle
guarantees an async paged operation must uphold — laziness, one exchange per
page, a close-once ledger over every termination path (success, error,
cancellation, early break), a single-use context-managed ``by_page`` view, and
the two ``__context__``-chaining behaviours around a failing close — plus the
async-only invariants the sync side cannot express: native cancellation
propagation (an ``asyncio.CancelledError`` is never converted), page-granular
drop-and-close of a staged page whose parse is cancelled, stack-safety over
many thousands of synchronously-completing pages, and original-cause surfacing
for a transport that throws eagerly before any ``await``.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import AsyncIterator

import pytest

from dexpace.sdk.core.errors import SdkError
from dexpace.sdk.core.http.common import Headers, MediaType, Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Status
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.pagination import AsyncPaginator, CursorStrategy, Page
from dexpace.sdk.core.pagination.strategy import HasHeaders


class _LedgerBody(AsyncResponseBody):
    """Async body that records only lifecycle (`close`) invocations.

    Reading it does not close it — ``bytes`` / ``string`` are overridden to
    hand back the buffered data without routing through the base class's
    close-on-exhaustion path. That isolates the close ledger to the closes
    driven by `Page.aclose` / `AsyncResponse.close`, so a count of exactly one
    per page proves the page owns and performs its close exactly once.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closes = 0

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        yield self._data

    async def bytes(self) -> bytes:
        return self._data

    async def string(self, encoding: str | None = None) -> str:
        return self._data.decode(encoding or "utf-8")

    async def close(self) -> None:
        self.closes += 1


class _ExplodingCloseBody(_LedgerBody):
    """A ledger body whose `close` records the call and then raises."""

    def __init__(self, data: bytes, error: BaseException) -> None:
        super().__init__(data)
        self._error = error

    async def close(self) -> None:
        self.closes += 1
        raise self._error


class _BlockingReadBody(AsyncResponseBody):
    """A body whose read parks on a gate and never self-closes on read.

    ``string`` blocks awaiting the gate, so a task parked mid-parse can be
    cancelled there. Because the read path does not close the body, the only
    close observable is the one the paginator drives when it drops the staged
    response — which is exactly what the mid-parse cancellation test asserts.
    """

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate
        self.closes = 0

    def media_type(self) -> MediaType | None:
        return None

    def content_length(self) -> int:
        return -1

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        await self._gate.wait()
        yield b'{"data": []}'

    async def bytes(self) -> bytes:
        await self._gate.wait()
        return b'{"data": []}'

    async def string(self, encoding: str | None = None) -> str:
        return (await self.bytes()).decode(encoding or "utf-8")

    async def close(self) -> None:
        self.closes += 1


class _Source:
    """Exchange-counting async pipeline mapping a cursor to a canned page body.

    Modelled as an ``AsyncPipelineLike`` (an ``async run``) rather than a bare
    callable: ``inspect.iscoroutinefunction`` reports ``False`` for a callable
    *instance* whose ``__call__`` is async, so a class with an async ``run`` is
    the async paginator's supported recording shape. Records every request it is
    handed so tests can assert exactly how many exchanges the paginator drove,
    and retains each body so the close ledger is observable.
    """

    def __init__(self, pages: dict[str | None, dict[str, object]]) -> None:
        self._pages = pages
        self.calls: list[Request] = []
        self.bodies: list[_LedgerBody] = []

    async def run(self, request: Request, _dispatch: DispatchContext) -> AsyncResponse:
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


def _response(request: Request, body: AsyncResponseBody) -> AsyncResponse:
    return AsyncResponse(
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


async def _drain(paginator: AsyncPaginator[int]) -> list[int]:
    return [item async for item in paginator]


# --- laziness / exchange counting -----------------------------------------


async def test_no_exchange_until_consumption_starts() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    paginator.__aiter__()  # obtaining the iterator must not fetch
    assert source.calls == []


async def test_by_page_does_not_fetch_until_iterated() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    paginator.by_page()  # building the page view must not fetch
    assert source.calls == []


async def test_exactly_one_exchange_per_page_and_no_overrun() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    assert await _drain(paginator) == [1, 2, 3, 4, 5]
    assert len(source.calls) == 3


async def test_each_aiter_restarts_pagination_from_the_start() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    assert await _drain(paginator) == [1, 2, 3, 4, 5]
    assert await _drain(paginator) == [1, 2, 3, 4, 5]
    # Two independent full walks, each with its own exchanges.
    assert len(source.calls) == 6


# --- close-once ledger over every abandon path ----------------------------


async def test_flat_iteration_closes_each_response_exactly_once() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    await _drain(paginator)
    assert [body.closes for body in source.bodies] == [1, 1, 1]


async def test_flat_item_view_closes_page_before_yielding_its_items() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    ait = paginator.__aiter__()
    assert await ait.__anext__() == 1
    # The first page's response is closed by the time its first item reaches the
    # caller — items are detached from any live connection.
    assert source.bodies[0].closes == 1


async def test_by_page_context_exit_closes_each_page_once() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    async with paginator.by_page() as pages:
        async for page in pages:
            async with page:
                _ = list(page.items)
    assert [body.closes for body in source.bodies] == [1, 1, 1]


async def test_by_page_early_break_releases_held_page_once() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    async with paginator.by_page() as pages:
        async for _page in pages:
            break  # hold the first page without closing it
    # Only the first page was fetched (laziness) and it is closed exactly once.
    assert len(source.bodies) == 1
    assert source.bodies[0].closes == 1


async def test_by_page_exhaustion_closes_every_page_once() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    async with paginator.by_page() as pages:
        async for page in pages:
            await page.aclose()
    assert [body.closes for body in source.bodies] == [1, 1, 1]


async def test_fetcher_does_not_close_pages_it_yields() -> None:
    # by_page yields live pages; closing is the caller's / page's job, never the
    # fetcher's. Without a close from the caller the responses stay open.
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    pages = [page async for page in paginator.by_page()]
    assert [body.closes for body in source.bodies] == [0, 0, 0]
    assert len(pages) == 3


# --- single-use, context-managed page view --------------------------------


async def test_by_page_is_single_use() -> None:
    source = _Source(_three_pages())
    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    paginator.by_page()
    with pytest.raises(RuntimeError, match="single-use"):
        paginator.by_page()


# --- error / __context__ chaining -----------------------------------------


async def test_parse_failure_closes_inline_with_parse_error_primary() -> None:
    parse_error = RuntimeError("parse boom")
    close_error = OSError("close boom")
    body = _ExplodingCloseBody(b'{"data": []}', close_error)

    async def source(request: Request) -> AsyncResponse:
        return _response(request, body)

    paginator: AsyncPaginator[int] = AsyncPaginator(
        source,
        _BoomStrategy(parse_error),
        _first_request(),
    )
    with pytest.raises(RuntimeError) as info:
        await _drain(paginator)
    # Parse error is primary; the close-time error is suppressed onto its
    # __context__, never masking the parse failure. The response is closed once.
    assert info.value is parse_error
    assert info.value.__context__ is close_error
    assert body.closes == 1


async def test_held_page_close_error_wraps_the_original_failure() -> None:
    close_error = OSError("close boom")
    payload = json.dumps({"data": [1], "next_cursor": None}).encode()
    body = _ExplodingCloseBody(payload, close_error)

    async def source(request: Request) -> AsyncResponse:
        return _response(request, body)

    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    original = RuntimeError("work boom")
    with pytest.raises(OSError) as info:
        async with paginator.by_page() as pages:
            async for _page in pages:
                raise original  # fails while holding a fetched-but-unconsumed page
    # The close-time error surfaces (wrapped); the original failure is the
    # secondary note on __context__.
    assert info.value is close_error
    assert info.value.__context__ is original


async def test_successful_path_close_error_is_surfaced() -> None:
    close_error = OSError("close boom")
    payload = json.dumps({"data": [1], "next_cursor": None}).encode()
    body = _ExplodingCloseBody(payload, close_error)

    async def source(request: Request) -> AsyncResponse:
        return _response(request, body)

    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    # The page parses and its items materialise fine; only its eager close on
    # the flat item view throws. That close error must surface to the caller
    # rather than being silently swallowed.
    with pytest.raises(OSError) as info:
        await _drain(paginator)
    assert info.value is close_error
    assert body.closes == 1


# --- eager (pre-await) transport throw ------------------------------------


async def test_eager_transport_throw_surfaces_original_cause() -> None:
    boom = RuntimeError("eager transport boom")

    async def source(request: Request) -> AsyncResponse:
        raise boom  # raised eagerly, before the coroutine ever awaits

    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    with pytest.raises(RuntimeError) as info:
        await _drain(paginator)
    assert info.value is boom


# --- cancellation matrix ---------------------------------------------------


async def test_mid_fetch_cancel_propagates_natively() -> None:
    gate = asyncio.Event()

    async def source(request: Request) -> AsyncResponse:
        await gate.wait()  # parked in-flight; the exchange never completes
        raise AssertionError("unreachable")

    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    task = asyncio.ensure_future(_drain(paginator))
    await asyncio.sleep(0)  # let the task park in the fetch
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_mid_parse_cancel_drops_and_closes_staged_page() -> None:
    gate = asyncio.Event()
    body = _BlockingReadBody(gate)

    async def source(request: Request) -> AsyncResponse:
        return _response(request, body)

    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    task = asyncio.ensure_future(_drain(paginator))
    await asyncio.sleep(0)  # let the task park mid-parse on the blocked read
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The staged response — never yielded as a page — is dropped and closed.
    assert body.closes == 1


async def test_cancelled_error_is_never_converted() -> None:
    async def source(request: Request) -> AsyncResponse:
        raise asyncio.CancelledError

    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    with pytest.raises(asyncio.CancelledError) as info:
        await _drain(paginator)
    # Exact type — never widened to an SdkError or any other exception type.
    assert type(info.value) is asyncio.CancelledError
    assert not isinstance(info.value, SdkError)


# --- trampoline over many synchronously-completing pages ------------------


async def test_trampoline_over_many_pages_has_no_stack_growth() -> None:
    page_count = 8000
    depths: list[int] = []

    async def source(request: Request) -> AsyncResponse:
        depths.append(len(traceback.extract_stack()))
        cursor = request.url.query.get("cursor")
        idx = 0 if cursor is None else int(cursor)
        nxt = None if idx >= page_count - 1 else str(idx + 1)
        payload = {"data": [idx], "next_cursor": nxt}
        return _response(request, _LedgerBody(json.dumps(payload).encode("utf-8")))

    paginator: AsyncPaginator[int] = AsyncPaginator(source, _strategy(), _first_request())
    items = await _drain(paginator)
    assert items == list(range(page_count))
    assert len(depths) == page_count
    # Constant stack depth across every page proves iteration, not per-page
    # recursion: a recursive drive would grow the stack linearly in the count.
    assert max(depths) - min(depths) <= 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
