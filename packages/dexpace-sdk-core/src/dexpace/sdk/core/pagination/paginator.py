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
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator, Iterator
from contextlib import aclosing
from types import TracebackType
from typing import TYPE_CHECKING, Self

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


def _validate_max_pages(max_pages: int | None) -> None:
    """Reject a non-positive page cap at construction.

    Args:
        max_pages: The requested cap, or ``None`` for an unbounded walk.

    Raises:
        ValueError: When ``max_pages`` is zero or negative. A cap of zero would
            fetch nothing and a negative cap is always a mistake; both are
            rejected up front rather than silently yielding an empty sequence.
    """
    if max_pages is not None and max_pages <= 0:
        raise ValueError(f"max_pages must be a positive integer or None, got {max_pages!r}")


def _close_suppressing(response: Response, primary: BaseException) -> None:
    """Close ``response`` while keeping ``primary`` the exception that surfaces.

    Used on the parse-failure path, where the response never became a page and
    so must be closed here. The parse error stays primary: any error the close
    raises is captured and hung on the parse error's ``__context__`` rather
    than replacing it, so the parse failure is never masked.

    Args:
        response: The response to close.
        primary: The in-flight parse error that must remain primary.
    """
    try:
        response.close()
    except BaseException as close_error:
        # Re-attached to the parse error below, never swallowed.
        close_error.__context__ = None
        primary.__context__ = close_error


async def _aclose_suppressing(response: AsyncResponse, primary: BaseException) -> None:
    """Async twin of `_close_suppressing`: close ``response`` keeping ``primary`` primary.

    Used on the async parse-failure and page-granular-cancellation paths, where
    the response never became a page and so must be closed here. An ordinary
    close *failure* is captured onto ``primary``'s ``__context__`` rather than
    replacing it, so the parse (or cancellation) error stays primary. A
    ``BaseException`` from the close — an ``asyncio.CancelledError`` reasserted
    by the shielded body close — is deliberately not suppressed: it propagates
    so a cancellation is never swallowed. ``AsyncResponse.close`` shields its
    body close, so the transport handle is released even while a cancellation
    is already unwinding.

    Args:
        response: The response to close.
        primary: The in-flight parse or cancellation error that must stay
            primary.
    """
    try:
        await response.close()
    except Exception as close_error:
        # An ordinary close failure is chained, never allowed to mask primary.
        close_error.__context__ = None
        primary.__context__ = close_error


class _PageView[T]:
    """Single-use, context-managed iterator over a paginator's pages.

    Wraps the paginator's lazy page generator. Iterating yields each `Page`;
    the caller owns each page's lifecycle (close it, or use it as a context
    manager). On context-manager exit — normal end, early ``break``, or an
    exception unwinding through the block — the page currently held (fetched
    but not yet released by the caller) is closed so no live response leaks. A
    close error raised there surfaces as the primary exception, wrapping any
    in-flight failure on its ``__context__``.
    """

    __slots__ = ("_current", "_pages")

    def __init__(self, pages: Generator[Page[T], None, None]) -> None:
        self._pages = pages
        self._current: Page[T] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._release()

    def __iter__(self) -> Iterator[Page[T]]:
        return self

    def __next__(self) -> Page[T]:
        try:
            page = next(self._pages)
        except StopIteration:
            self._current = None
            raise
        self._current = page
        return page

    def _release(self) -> None:
        """Close the held page (if any) and stop the underlying generator."""
        held, self._current = self._current, None
        try:
            if held is not None:
                held.close()
        finally:
            self._pages.close()


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

    __slots__ = (
        "_dispatch_factory",
        "_initial",
        "_max_pages",
        "_page_view_taken",
        "_send",
        "_strategy",
    )

    def __init__(
        self,
        source: SyncPipelineLike | SendSync,
        strategy: PaginationStrategy[T],
        initial_request: Request,
        *,
        max_pages: int | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        _validate_max_pages(max_pages)
        self._strategy = strategy
        self._initial = initial_request
        self._max_pages = max_pages
        self._dispatch_factory = dispatch_factory or DispatchContext.noop
        self._page_view_taken = False
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

    def _pages(self) -> Generator[Page[T], None, None]:
        """Fetch and parse each page lazily, one exchange per page consumed.

        Nothing is fetched until the first page is pulled, exactly one exchange
        runs per page, and the walk stops at the terminal page or the
        ``max_pages`` cap — never overrunning either. The fetcher never closes a
        page it produced; a live page is closed by the item view, the page-view
        release, or the caller. A response that fails to parse never becomes a
        page, so `_parse` closes it inline.

        Yields:
            Each parsed `Page`, first to last.
        """
        request: Request | None = self._initial
        count = 0
        while request is not None:
            if self._max_pages is not None and count >= self._max_pages:
                return
            page = self._parse(self._send(request))
            count += 1
            yield page
            request = page.next_request

    def by_page(self) -> _PageView[T]:
        """Return the single-use, context-managed page view.

        The view yields each `Page` in turn, honouring ``max_pages``. Because
        each page owns a live response, the view is single-use — a second call
        raises — and is a context manager: on exit (normal, early ``break``, or
        an exception) the page still held is closed so no response leaks.

        Returns:
            The page view. Iterate it within a ``with`` block; close each page
            (or use it as a context manager) to release its connection.

        Raises:
            RuntimeError: If a page view was already taken from this paginator.
        """
        if self._page_view_taken:
            raise RuntimeError(
                "by_page() is single-use; build a fresh Paginator for another page walk",
            )
        self._page_view_taken = True
        return _PageView(self._pages())

    def _parse(self, response: Response) -> Page[T]:
        """Decode and parse ``response`` into a `Page`, closing it on failure.

        On success the page owns the response and closes it later. On any
        failure reading, decoding, or parsing, the response — which never became
        a page — is closed inline, with the parse error kept primary and any
        close-time error suppressed onto its ``__context__``.
        """
        try:
            raw = response.body.string() if response.body is not None else None
            payload = _decode_for(raw, response.request)
            return self._strategy.parse(response, payload, response.request)
        except BaseException as parse_error:
            _close_suppressing(response, parse_error)
            raise

    def __iter__(self) -> Iterator[T]:
        """Iterate items across all pages, eager-closing each page first.

        Each page's response is closed before its items are yielded, so items
        reach the caller already detached from any live connection. A fresh
        call restarts pagination from the initial request, so two ``iter``
        calls perform two independent walks with their own exchanges.

        Yields:
            Items in server order across every page.
        """
        for page in self._pages():
            page.close()
            yield from page.items


class _AsyncPageView[T]:
    """Single-use, async-context-managed iterator over a paginator's pages.

    Async twin of `_PageView`. Wraps the paginator's lazy async page generator.
    Async-iterating yields each `Page`; the caller owns each page's lifecycle
    (``await page.aclose()``, or ``async with page``). On async-context-manager
    exit — normal end, early ``break``, an exception, or a cancellation
    unwinding through the block — the page currently held (fetched but not yet
    released by the caller) is closed so no live response leaks. A close error
    raised there surfaces as the primary exception, wrapping any in-flight
    failure on its ``__context__``.
    """

    __slots__ = ("_current", "_pages")

    def __init__(self, pages: AsyncGenerator[Page[T], None]) -> None:
        self._pages = pages
        self._current: Page[T] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._release()

    def __aiter__(self) -> AsyncIterator[Page[T]]:
        return self

    async def __anext__(self) -> Page[T]:
        try:
            page = await self._pages.__anext__()
        except StopAsyncIteration:
            self._current = None
            raise
        self._current = page
        return page

    async def _release(self) -> None:
        """Close the held page (if any), then stop the underlying generator."""
        held, self._current = self._current, None
        try:
            if held is not None:
                await held.aclose()
        finally:
            await self._pages.aclose()


class AsyncPaginator[T]:
    """Asynchronous twin of `Paginator`.

    Mirrors the sync paginator with ``async`` iteration semantics: lazy per-page
    fetching, exactly one exchange per page, close-once discipline over every
    termination path (success, error, cancellation, early break), and a
    single-use ``by_page`` view. ``source`` is an ``AsyncPipeline`` or an async
    send-callable. Cancellation propagates natively — an
    ``asyncio.CancelledError`` raised while a page is being fetched or parsed
    unwinds unconverted, and a page staged but not yet yielded is dropped and
    closed before the cancellation continues.
    """

    __slots__ = (
        "_dispatch_factory",
        "_initial",
        "_max_pages",
        "_page_view_taken",
        "_send",
        "_strategy",
    )

    def __init__(
        self,
        source: AsyncPipelineLike | SendAsync,
        strategy: PaginationStrategy[T],
        initial_request: Request,
        *,
        max_pages: int | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        _validate_max_pages(max_pages)
        self._strategy = strategy
        self._initial = initial_request
        self._max_pages = max_pages
        self._dispatch_factory = dispatch_factory or DispatchContext.noop
        self._page_view_taken = False
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

    async def _apages(self) -> AsyncGenerator[Page[T], None]:
        """Fetch and parse each page lazily, one exchange per page consumed.

        Nothing is fetched until the first page is pulled, exactly one exchange
        runs per page, and the walk stops at the terminal page or the
        ``max_pages`` cap. The fetcher never closes a page it produced; a live
        page is closed by the item view, the page-view release, or the caller. A
        response that fails to parse — or whose parse is cancelled — never
        becomes a page, so `_aparse` closes it inline.

        Yields:
            Each parsed `Page`, first to last.
        """
        request: Request | None = self._initial
        count = 0
        while request is not None:
            if self._max_pages is not None and count >= self._max_pages:
                return
            page = await self._aparse(await self._send(request))
            count += 1
            yield page
            request = page.next_request

    def by_page(self) -> _AsyncPageView[T]:
        """Return the single-use, async-context-managed page view.

        The view async-yields each `Page` in turn, honouring ``max_pages``.
        Because each page owns a live response, the view is single-use — a
        second call raises — and is an async context manager: on exit (normal,
        early ``break``, an exception, or a cancellation) the page still held is
        closed so no response leaks.

        Returns:
            The page view. Iterate it within an ``async with`` block; close each
            page (``await page.aclose()`` or ``async with page``) to release its
            connection.

        Raises:
            RuntimeError: If a page view was already taken from this paginator.
        """
        if self._page_view_taken:
            raise RuntimeError(
                "by_page() is single-use; build a fresh AsyncPaginator for another page walk",
            )
        self._page_view_taken = True
        return _AsyncPageView(self._apages())

    async def _aparse(self, response: AsyncResponse) -> Page[T]:
        """Decode and parse ``response`` into a `Page`, closing it on failure.

        On success the page owns the response and closes it later. On any
        failure reading, decoding, or parsing — or an ``asyncio.CancelledError``
        raised mid-parse — the response, which never became a page, is closed
        inline, with the primary error kept primary and any ordinary close-time
        error suppressed onto its ``__context__``. The shielded close releases
        the transport handle even while a cancellation unwinds.
        """
        try:
            raw = await response.body.string() if response.body is not None else None
            payload = _decode_for(raw, response.request)
            return self._strategy.parse(response, payload, response.request)
        except BaseException as parse_error:
            await _aclose_suppressing(response, parse_error)
            raise

    def __aiter__(self) -> AsyncIterator[T]:
        """Iterate items across all pages, eager-closing each page first.

        Each page's response is closed before its items are yielded, so items
        reach the caller already detached from any live connection. A fresh
        ``aiter`` restarts pagination from the initial request, so two async
        iterations perform two independent walks with their own exchanges.

        Yields:
            Items in server order across every page.
        """
        return self._iterate_items()

    async def _iterate_items(self) -> AsyncIterator[T]:
        async with aclosing(self._apages()) as pages:
            async for page in pages:
                await page.aclose()
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
