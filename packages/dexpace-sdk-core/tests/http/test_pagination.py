# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``Pager`` / ``ItemPaged`` and their async twins."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import pytest

from dexpace.sdk.core.errors import SdkError
from dexpace.sdk.core.http.common import AsyncItemPaged, AsyncPager, ItemPaged, Pager


@dataclass(frozen=True, slots=True)
class _Page:
    items: list[int]
    next_token: str | None


def _build_get_next(
    pages: dict[str | None, _Page],
) -> Callable[[str | None], _Page]:
    def _get_next(token: str | None) -> _Page:
        if token not in pages:
            raise ValueError(f"unknown page token: {token!r}")
        return pages[token]

    return _get_next


def _extract(page: _Page) -> tuple[str | None, Iterable[int]]:
    return page.next_token, page.items


class TestPager:
    def test_iterates_pages_in_order(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1, 2], next_token="page2"),
            "page2": _Page(items=[3], next_token=None),
        }
        pager: Pager[int, _Page] = Pager(_build_get_next(pages), _extract)
        collected = [list(page) for page in pager]
        assert collected == [[1, 2], [3]]

    def test_continuation_token_propagates_to_error(self) -> None:
        def _failing(token: str | None) -> _Page:
            raise SdkError(f"page {token} failed")

        pager: Pager[int, _Page] = Pager(_failing, _extract, continuation_token="abc")
        with pytest.raises(SdkError) as info:
            list(pager)
        assert info.value.continuation_token == "abc"

    def test_extract_data_failure_stamps_continuation_token(self) -> None:
        # A failure inside extract_data (not just get_next) must also stamp the
        # current continuation token so the caller can resume from that page.
        pages: dict[str | None, _Page] = {"resume": _Page(items=[1], next_token=None)}

        def _bad_extract(_page: _Page) -> tuple[str | None, Iterable[int]]:
            raise SdkError("extract failed")

        pager: Pager[int, _Page] = Pager(
            _build_get_next(pages),
            _bad_extract,
            continuation_token="resume",
        )
        with pytest.raises(SdkError) as info:
            list(pager)
        assert info.value.continuation_token == "resume"

    def test_max_pages_bounds_iteration(self) -> None:
        # A buggy server returning the same token forever must not loop
        # indefinitely; max_pages caps the number of pages produced.
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1], next_token="loop"),
            "loop": _Page(items=[2], next_token="loop"),
        }
        pager: Pager[int, _Page] = Pager(_build_get_next(pages), _extract, max_pages=3)
        collected = [list(page) for page in pager]
        assert collected == [[1], [2], [2]]


class TestItemPaged:
    def test_flat_iteration(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1, 2], next_token="x"),
            "x": _Page(items=[3, 4], next_token=None),
        }
        items: ItemPaged[int, _Page] = ItemPaged(_build_get_next(pages), _extract)
        assert list(items) == [1, 2, 3, 4]

    def test_by_page(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1], next_token=None),
        }
        items: ItemPaged[int, _Page] = ItemPaged(_build_get_next(pages), _extract)
        result = [list(page) for page in items.by_page()]
        assert result == [[1]]

    def test_by_page_with_continuation_token(self) -> None:
        # Forwarding a continuation token to by_page must resume from that page
        # without colliding with positional get_next/extract_data arguments.
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1], next_token="page2"),
            "page2": _Page(items=[2, 3], next_token=None),
        }
        items: ItemPaged[int, _Page] = ItemPaged(_build_get_next(pages), _extract)
        result = [list(page) for page in items.by_page(continuation_token="page2")]
        assert result == [[2, 3]]

    def test_max_pages_bounds_flat_iteration(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1], next_token="loop"),
            "loop": _Page(items=[2], next_token="loop"),
        }
        items: ItemPaged[int, _Page] = ItemPaged(
            _build_get_next(pages),
            _extract,
            max_pages=2,
        )
        assert list(items) == [1, 2]

    def test_max_pages_is_forwarded_to_by_page(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1], next_token="loop"),
            "loop": _Page(items=[2], next_token="loop"),
        }
        items: ItemPaged[int, _Page] = ItemPaged(
            _build_get_next(pages),
            _extract,
            max_pages=2,
        )
        assert [list(page) for page in items.by_page()] == [[1], [2]]


class TestAsyncPager:
    async def test_iterates_pages(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1, 2], next_token="next"),
            "next": _Page(items=[3], next_token=None),
        }

        async def get_next(token: str | None) -> _Page:
            return pages[token]

        async def extract(page: _Page) -> tuple[str | None, Iterable[int]]:
            return page.next_token, page.items

        pager: AsyncPager[int, _Page] = AsyncPager(get_next, extract)
        collected: list[list[int]] = []
        async for page in pager:
            collected.append([item async for item in page])
        assert collected == [[1, 2], [3]]

    async def test_max_pages_bounds_iteration(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1], next_token="loop"),
            "loop": _Page(items=[2], next_token="loop"),
        }

        async def get_next(token: str | None) -> _Page:
            return pages[token]

        async def extract(page: _Page) -> tuple[str | None, Iterable[int]]:
            return page.next_token, page.items

        pager: AsyncPager[int, _Page] = AsyncPager(get_next, extract, max_pages=2)
        collected: list[list[int]] = []
        async for page in pager:
            collected.append([item async for item in page])
        assert collected == [[1], [2]]

    async def test_extract_data_failure_stamps_continuation_token(self) -> None:
        async def get_next(_token: str | None) -> _Page:
            return _Page(items=[1], next_token=None)

        async def extract(_page: _Page) -> tuple[str | None, Iterable[int]]:
            raise SdkError("extract failed")

        pager: AsyncPager[int, _Page] = AsyncPager(
            get_next,
            extract,
            continuation_token="resume",
        )
        with pytest.raises(SdkError) as info:
            async for _page in pager:
                pass
        assert info.value.continuation_token == "resume"


class TestAsyncItemPaged:
    async def test_flat_iteration(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1, 2], next_token="next"),
            "next": _Page(items=[3], next_token=None),
        }

        async def get_next(token: str | None) -> _Page:
            return pages[token]

        async def extract(page: _Page) -> tuple[str | None, Iterable[int]]:
            return page.next_token, page.items

        items: AsyncItemPaged[int, _Page] = AsyncItemPaged(get_next, extract)
        collected: list[int] = []
        async for item in items:
            collected.append(item)
        assert collected == [1, 2, 3]

    async def test_by_page_with_continuation_token(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1], next_token="next"),
            "next": _Page(items=[2, 3], next_token=None),
        }

        async def get_next(token: str | None) -> _Page:
            return pages[token]

        async def extract(page: _Page) -> tuple[str | None, Iterable[int]]:
            return page.next_token, page.items

        items: AsyncItemPaged[int, _Page] = AsyncItemPaged(get_next, extract)
        collected: list[list[int]] = []
        async for page in items.by_page(continuation_token="next"):
            collected.append([item async for item in page])
        assert collected == [[2, 3]]

    async def test_max_pages_bounds_flat_iteration(self) -> None:
        pages: dict[str | None, _Page] = {
            None: _Page(items=[1], next_token="loop"),
            "loop": _Page(items=[2], next_token="loop"),
        }

        async def get_next(token: str | None) -> _Page:
            return pages[token]

        async def extract(page: _Page) -> tuple[str | None, Iterable[int]]:
            return page.next_token, page.items

        items: AsyncItemPaged[int, _Page] = AsyncItemPaged(get_next, extract, max_pages=2)
        collected: list[int] = []
        async for item in items:
            collected.append(item)
        assert collected == [1, 2]
