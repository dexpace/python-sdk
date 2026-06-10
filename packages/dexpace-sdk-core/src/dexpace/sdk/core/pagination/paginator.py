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

Use `Paginator.by_page` when the raw response or page boundaries matter::

    for page in Paginator(pipeline, strategy, first_request).by_page():
        with page:
            process(page.items, page.raw)

The optional ``max_pages`` guard bounds how many pages are fetched — essential
when draining an unbounded server-side sequence.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import TYPE_CHECKING

from ..errors import DeserializationError
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
    """Decode a JSON body string into a Python value (``None`` when empty).

    Args:
        raw: The response body text. An empty or whitespace-only body
            decodes to ``None``.

    Returns:
        The decoded Python value, or ``None`` for an empty body.

    Raises:
        DeserializationError: When the body is not well-formed JSON (e.g. an
            HTML error page returned with a 200 by a load balancer). The
            underlying ``json.JSONDecodeError`` never escapes the SDK error
            hierarchy.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise DeserializationError("pagination response body is not valid JSON") from err


def _decode_for(raw: str | None, request: Request) -> object:
    """Decode a page body, stamping the failing request for resumption.

    Mirrors ``Pager``'s resume contract: when decoding fails, the request
    URL that produced the unparseable page is stamped onto the
    ``DeserializationError`` (as its ``continuation_token``) so a caller can
    rebuild the same request and retry from exactly that page rather than
    restarting the whole sequence.

    Args:
        raw: The response body text, or ``None`` when the response had no
            body.
        request: The request that produced ``raw``; its URL becomes the
            resume token on a decode failure.

    Returns:
        The decoded Python value, or ``None`` for an absent or empty body.

    Raises:
        DeserializationError: When the body is not well-formed JSON.
    """
    if raw is None:
        return None
    try:
        return _decode_body(raw)
    except DeserializationError as err:
        if err.continuation_token is None:
            err.continuation_token = str(request.url)
        raise


class Paginator[T]:
    """Synchronous paginator over a strategy-defined page sequence.

    Args:
        source: Either a ``Pipeline`` (run once per page with a fresh
            dispatch context) or a send-callable ``Request -> Response``.
        strategy: The `PaginationStrategy` that parses each response
            into a `Page`.
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
            if inspect.iscoroutinefunction(pipeline.run):
                raise TypeError(
                    "Paginator was given an async pipeline; its run() is a "
                    "coroutine function. Use AsyncPaginator for async pipelines.",
                )

            def send(request: Request) -> Response:
                return pipeline.run(request, self._dispatch_factory())

            return send
        if inspect.iscoroutinefunction(source):
            raise TypeError(
                "Paginator was given an async send-callable; use AsyncPaginator.",
            )
        return source

    def by_page(self) -> Iterator[Page[T]]:
        """Yield each `Page` in turn, honouring ``max_pages``.

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
        raw = response.body.string() if response.body is not None else None
        payload = _decode_for(raw, response.request)
        return self._strategy.parse(response, payload, response.request)

    def __iter__(self) -> Iterator[T]:
        for page in self.by_page():
            with page:
                yield from page.items


class AsyncPaginator[T]:
    """Asynchronous twin of `Paginator`.

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
            if not inspect.iscoroutinefunction(pipeline.run):
                raise TypeError(
                    "AsyncPaginator was given a sync pipeline; its run() is "
                    "not a coroutine function. Use Paginator for sync pipelines.",
                )

            async def send(request: Request) -> AsyncResponse:
                return await pipeline.run(request, self._dispatch_factory())

            return send
        if not inspect.iscoroutinefunction(source):
            raise TypeError(
                "AsyncPaginator was given a sync send-callable; use Paginator.",
            )
        return source

    async def by_page(self) -> AsyncIterator[Page[T]]:
        """Async-yield each `Page` in turn, honouring ``max_pages``."""
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
        raw = await response.body.string() if response.body is not None else None
        payload = _decode_for(raw, response.request)
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
