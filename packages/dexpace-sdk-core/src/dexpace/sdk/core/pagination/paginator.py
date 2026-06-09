# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Paginators — iterate a paged operation item-by-item or page-by-page.

A paginator drives the **pipeline**, not a bare transport: each page fetch is
sent through the full policy chain, so retry, auth-refresh, redirect, and
tracing apply to every page automatically. The caller supplies either a
``Pipeline`` / ``AsyncPipeline`` (the paginator runs it with a fresh dispatch
context per page) or a plain send-callable for full control.

Iteration is item-by-item by default::

    for item in Paginator(pipeline, strategy, first_request):
        ...

Use :meth:`Paginator.by_page` when the raw response or page boundaries matter::

    for page in Paginator(pipeline, strategy, first_request).by_page():
        with page:
            process(page.items, page.raw)

The optional ``max_pages`` guard bounds how many pages are fetched — essential
when draining an unbounded server-side sequence.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import TYPE_CHECKING

from ..http.context.dispatch_context import DispatchContext
from ..pipeline.dispatch import (
    AsyncPipelineLike,
    SendAsync,
    SendSync,
    SyncPipelineLike,
)
from .page import Page

if TYPE_CHECKING:
    from ..http.request.request import Request
    from ..http.response.async_response import AsyncResponse
    from ..http.response.response import Response
    from .strategy import PaginationStrategy


def _decode_body(raw: str) -> object:
    """Decode a JSON body string into a Python value (``None`` when empty)."""
    text = raw.strip()
    if not text:
        return None
    return json.loads(text)


class Paginator[T]:
    """Synchronous paginator over a strategy-defined page sequence.

    Args:
        source: Either a ``Pipeline`` (run once per page with a fresh
            dispatch context) or a send-callable ``Request -> Response``.
        strategy: The :class:`PaginationStrategy` that parses each response
            into a :class:`Page`.
        initial_request: The request that fetches the first page.
        max_pages: Optional cap on the number of pages fetched. ``None``
            means unbounded (drive it with care against open-ended APIs).
        dispatch_factory: Builds the dispatch context for each page when
            ``source`` is a ``Pipeline``. Defaults to ``DispatchContext.noop``.
    """

    __slots__ = ("_dispatch_factory", "_initial", "_max_pages", "_send", "_strategy")

    def __init__(
        self,
        source: SyncPipelineLike | SendSync,
        strategy: PaginationStrategy[T],
        initial_request: Request,
        *,
        max_pages: int | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        self._strategy = strategy
        self._initial = initial_request
        self._max_pages = max_pages
        self._dispatch_factory = dispatch_factory or DispatchContext.noop
        self._send = self._normalise(source)

    def _normalise(self, source: SyncPipelineLike | SendSync) -> SendSync:
        if isinstance(source, SyncPipelineLike):
            pipeline = source

            def send(request: Request) -> Response:
                return pipeline.run(request, self._dispatch_factory())

            return send
        return source

    def by_page(self) -> Iterator[Page[T]]:
        """Yield each :class:`Page` in turn, honouring ``max_pages``.

        Yields:
            Pages from first to last. Each page owns its response; iterate
            within a ``with page:`` block, or call ``page.close()``, to
            release the connection promptly.
        """
        request: Request | None = self._initial
        count = 0
        while request is not None:
            if self._max_pages is not None and count >= self._max_pages:
                return
            response = self._send(request)
            page = self._parse(response)
            count += 1
            yield page
            request = page.next_request

    def _parse(self, response: Response) -> Page[T]:
        payload = _decode_body(response.body.string()) if response.body is not None else None
        return self._strategy.parse(response, payload, response.request)

    def __iter__(self) -> Iterator[T]:
        for page in self.by_page():
            with page:
                yield from page.items


class AsyncPaginator[T]:
    """Asynchronous twin of :class:`Paginator`.

    Mirrors the sync paginator exactly with ``async`` iteration semantics.
    ``source`` is an ``AsyncPipeline`` or an async send-callable.
    """

    __slots__ = ("_dispatch_factory", "_initial", "_max_pages", "_send", "_strategy")

    def __init__(
        self,
        source: AsyncPipelineLike | SendAsync,
        strategy: PaginationStrategy[T],
        initial_request: Request,
        *,
        max_pages: int | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        self._strategy = strategy
        self._initial = initial_request
        self._max_pages = max_pages
        self._dispatch_factory = dispatch_factory or DispatchContext.noop
        self._send = self._normalise(source)

    def _normalise(self, source: AsyncPipelineLike | SendAsync) -> SendAsync:
        if isinstance(source, AsyncPipelineLike):
            pipeline = source

            async def send(request: Request) -> AsyncResponse:
                return await pipeline.run(request, self._dispatch_factory())

            return send
        return source

    async def by_page(self) -> AsyncIterator[Page[T]]:
        """Async-yield each :class:`Page` in turn, honouring ``max_pages``."""
        request: Request | None = self._initial
        count = 0
        while request is not None:
            if self._max_pages is not None and count >= self._max_pages:
                return
            response = await self._send(request)
            page = await self._parse(response)
            count += 1
            yield page
            request = page.next_request

    async def _parse(self, response: AsyncResponse) -> Page[T]:
        payload = _decode_body(await response.body.string()) if response.body is not None else None
        return self._strategy.parse(response, payload, response.request)

    def __aiter__(self) -> AsyncIterator[T]:
        return self._iterate_items()

    async def _iterate_items(self) -> AsyncIterator[T]:
        async for page in self.by_page():
            async with page:
                for item in page.items:
                    yield item


__all__ = [
    "AsyncPaginator",
    "AsyncPipelineLike",
    "Paginator",
    "SendAsync",
    "SendSync",
    "SyncPipelineLike",
]
