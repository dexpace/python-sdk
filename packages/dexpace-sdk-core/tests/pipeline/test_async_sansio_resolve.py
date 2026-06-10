# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Unit tests for the async SansIO runner's ``_resolve`` helper.

``_resolve`` awaits awaitable step results and passes plain values through
unchanged. Both async-callable steps (which return coroutines) and sync
callable steps (which return plain values) flow through the same code path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator

from dexpace.sdk.core.pipeline._async_sansio_runner import _resolve


async def test_resolve_passes_plain_value_through() -> None:
    assert await _resolve(42) == 42
    assert await _resolve(None) is None


async def test_resolve_awaits_a_coroutine() -> None:
    async def produce() -> str:
        return "awaited"

    assert await _resolve(produce()) == "awaited"


async def test_resolve_awaits_a_future() -> None:
    future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    future.set_result(7)
    assert await _resolve(future) == 7


async def test_resolve_awaits_a_custom_awaitable() -> None:
    class _Awaitable:
        def __await__(self) -> Generator[None, None, str]:
            yield from ()
            return "custom"

    assert await _resolve(_Awaitable()) == "custom"
