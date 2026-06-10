# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Generic pager iterators (sync + async).

Modelled on Azure's ``corehttp.paging``: caller supplies two callables —
``get_next(continuation_token)`` to fetch a page and ``extract_data(page)``
to extract the next token plus the items in that page. The iterator yields
items or pages depending on which iterator surface the consumer uses.
"""

from __future__ import annotations

import itertools
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
)

from ...errors import SdkError


class Pager[T, R](Iterator[Iterator[T]]):
    """Sync iterator of pages.

    Each ``__next__`` call returns an ``Iterator[T]`` over the items in
    the page that ``get_next`` produced. Iteration terminates when
    ``extract_data`` returns a ``None`` continuation token after producing
    at least one page, or when the optional ``max_pages`` bound is reached.

    On any ``SdkError`` raised by ``get_next`` or ``extract_data``, the
    current ``continuation_token`` is stamped onto the error so callers can
    resume from the page that failed.

    The optional ``max_pages`` guard caps how many pages are produced; it is
    the safety valve against a buggy server that returns the same
    continuation token forever, which would otherwise loop indefinitely.
    """

    __slots__ = (
        "_did_first_call",
        "_extract_data",
        "_get_next",
        "_max_pages",
        "_pages_yielded",
        "continuation_token",
    )

    def __init__(
        self,
        get_next: Callable[[str | None], R],
        extract_data: Callable[[R], tuple[str | None, Iterable[T]]],
        continuation_token: str | None = None,
        *,
        max_pages: int | None = None,
    ) -> None:
        self._get_next = get_next
        self._extract_data = extract_data
        self.continuation_token = continuation_token
        self._did_first_call = False
        self._max_pages = max_pages
        self._pages_yielded = 0

    def __iter__(self) -> Iterator[Iterator[T]]:
        return self

    def __next__(self) -> Iterator[T]:
        if self.continuation_token is None and self._did_first_call:
            raise StopIteration
        if self._max_pages is not None and self._pages_yielded >= self._max_pages:
            raise StopIteration
        try:
            response = self._get_next(self.continuation_token)
            self._did_first_call = True
            self.continuation_token, items = self._extract_data(response)
        except SdkError as err:
            if err.continuation_token is None:
                err.continuation_token = self.continuation_token
            raise
        self._pages_yielded += 1
        return iter(items)


class ItemPaged[T, R](Iterator[T]):
    """Flat iterator over the items of a paged response.

    Wraps a ``Pager`` and yields individual items rather than pages. Use
    ``by_page`` when page-level iteration is required.

    The second type parameter ``R`` is the page type produced by ``get_next``
    and consumed by ``extract_data``.
    """

    __slots__ = ("_extract_data", "_flat", "_get_next", "_max_pages")

    def __init__(
        self,
        get_next: Callable[[str | None], R],
        extract_data: Callable[[R], tuple[str | None, Iterable[T]]],
        *,
        max_pages: int | None = None,
    ) -> None:
        self._get_next = get_next
        self._extract_data = extract_data
        self._flat: Iterator[T] | None = None
        self._max_pages = max_pages

    def by_page(self, continuation_token: str | None = None) -> Iterator[Iterator[T]]:
        """Return a page-level iterator, optionally resuming from a token.

        Args:
            continuation_token: When set, resume paging from that page rather
                than the first.

        Returns:
            An iterator yielding one ``Iterator[T]`` per page. The
            ``max_pages`` bound supplied at construction is applied.
        """
        return Pager(
            self._get_next,
            self._extract_data,
            continuation_token=continuation_token,
            max_pages=self._max_pages,
        )

    def __iter__(self) -> Iterator[T]:
        return self

    def __next__(self) -> T:
        if self._flat is None:
            self._flat = itertools.chain.from_iterable(self.by_page())
        return next(self._flat)


class AsyncPager[T, R](AsyncIterator[AsyncIterator[T]]):
    """Async iterator of pages.

    On any ``SdkError`` raised by ``get_next`` or ``extract_data``, the
    current ``continuation_token`` is stamped onto the error so callers can
    resume from the page that failed. The optional ``max_pages`` guard caps
    how many pages are produced, guarding against a server that returns the
    same continuation token forever.
    """

    __slots__ = (
        "_did_first_call",
        "_extract_data",
        "_get_next",
        "_max_pages",
        "_pages_yielded",
        "continuation_token",
    )

    def __init__(
        self,
        get_next: Callable[[str | None], Awaitable[R]],
        extract_data: Callable[[R], Awaitable[tuple[str | None, Iterable[T]]]],
        continuation_token: str | None = None,
        *,
        max_pages: int | None = None,
    ) -> None:
        self._get_next = get_next
        self._extract_data = extract_data
        self.continuation_token = continuation_token
        self._did_first_call = False
        self._max_pages = max_pages
        self._pages_yielded = 0

    def __aiter__(self) -> AsyncIterator[AsyncIterator[T]]:
        return self

    async def __anext__(self) -> AsyncIterator[T]:
        if self.continuation_token is None and self._did_first_call:
            raise StopAsyncIteration
        if self._max_pages is not None and self._pages_yielded >= self._max_pages:
            raise StopAsyncIteration
        try:
            response = await self._get_next(self.continuation_token)
            self._did_first_call = True
            token, items = await self._extract_data(response)
        except SdkError as err:
            if err.continuation_token is None:
                err.continuation_token = self.continuation_token
            raise
        self.continuation_token = token
        self._pages_yielded += 1
        return _SyncToAsync(iter(items))


class AsyncItemPaged[T, R](AsyncIterator[T]):
    """Flat async iterator over items of a paged response.

    The second type parameter ``R`` is the page type produced by ``get_next``
    and consumed by ``extract_data``.
    """

    __slots__ = ("_current", "_extract_data", "_get_next", "_max_pages", "_pages")

    def __init__(
        self,
        get_next: Callable[[str | None], Awaitable[R]],
        extract_data: Callable[[R], Awaitable[tuple[str | None, Iterable[T]]]],
        *,
        max_pages: int | None = None,
    ) -> None:
        self._get_next = get_next
        self._extract_data = extract_data
        self._pages: AsyncIterator[AsyncIterator[T]] | None = None
        self._current: AsyncIterator[T] | None = None
        self._max_pages = max_pages

    def by_page(
        self,
        continuation_token: str | None = None,
    ) -> AsyncIterator[AsyncIterator[T]]:
        """Return a page-level async iterator, optionally resuming from a token.

        Args:
            continuation_token: When set, resume paging from that page rather
                than the first.

        Returns:
            An async iterator yielding one ``AsyncIterator[T]`` per page. The
            ``max_pages`` bound supplied at construction is applied.
        """
        return AsyncPager(
            self._get_next,
            self._extract_data,
            continuation_token=continuation_token,
            max_pages=self._max_pages,
        )

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        if self._pages is None:
            self._pages = self.by_page()
        while True:
            if self._current is None:
                self._current = await self._pages.__anext__()
            try:
                return await self._current.__anext__()
            except StopAsyncIteration:
                self._current = None


class _SyncToAsync[T](AsyncIterator[T]):
    """Wrap a sync iterator as an async iterator."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Iterator[T]) -> None:
        self._inner = inner

    async def __anext__(self) -> T:
        try:
            return next(self._inner)
        except StopIteration as err:
            raise StopAsyncIteration from err


__all__ = ["AsyncItemPaged", "AsyncPager", "ItemPaged", "Pager"]
