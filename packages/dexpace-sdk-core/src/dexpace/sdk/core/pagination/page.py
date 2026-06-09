# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""A single page of results produced by a ``PaginationStrategy``."""

from __future__ import annotations

from collections.abc import Coroutine, Iterator, Sequence
from dataclasses import dataclass, field
from inspect import isawaitable
from types import TracebackType
from typing import TYPE_CHECKING, Self, cast

if TYPE_CHECKING:
    from ..http.request.request import Request


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of items plus the requests that reach its neighbours.

    A strategy parses a response into a ``Page``: the ``items`` it carried,
    the ``next_request`` to fetch the following page (``None`` at the end of
    the sequence), and — when the API supports backward paging — an optional
    ``prev_request``. The originating response is retained as ``raw`` so the
    page can be used as a context manager that releases the underlying
    connection on exit.

    Both the synchronous and asynchronous context-manager protocols are
    implemented. ``__exit__`` closes a synchronous response; ``__aexit__``
    awaits an asynchronous one. Closing is idempotent and tolerates a ``raw``
    that exposes neither hook (e.g. a hand-built page in a test).

    Attributes:
        items: The items on this page, in server order.
        next_request: Request that fetches the next page, or ``None`` when
            this is the final page.
        prev_request: Request that fetches the previous page, when the API
            exposes one; ``None`` otherwise.
        raw: The originating response object (kept for connection cleanup and
            for callers that need headers / status off the underlying page).
    """

    items: Sequence[T]
    next_request: Request | None = None
    prev_request: Request | None = None
    raw: object | None = field(default=None, compare=False)

    @property
    def has_next(self) -> bool:
        """Whether a further page is reachable from this one."""
        return self.next_request is not None

    def close(self) -> None:
        """Close the underlying synchronous response, if any. Idempotent."""
        close = getattr(self.raw, "close", None)
        if close is None:
            return
        result = close()
        if isawaitable(result):
            # An async response was stored on a sync page; it cannot be
            # awaited here, so close the coroutine to avoid a "never awaited"
            # warning and defer real cleanup to the async exit path.
            cast(Coroutine[object, object, object], result).close()

    async def aclose(self) -> None:
        """Close the underlying asynchronous response, if any. Idempotent."""
        close = getattr(self.raw, "close", None)
        if close is None:
            return
        result = close()
        if isawaitable(result):
            await result

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)


__all__ = ["Page"]
