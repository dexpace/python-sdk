# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async SansIO runners — mirror the sync ``_SansIO*Runner`` shapes.

Async SansIO steps may be plain callables (``(value, ctx) -> value``) or
async callables (``async def``); both forms are supported. Detection keys off
the *returned value* rather than the callable: ``_resolve`` awaits anything
that is an ``Awaitable`` (coroutines, futures, and custom awaitables alike)
and passes plain values through unchanged. Inspecting the result rather than
the callable means wrapped, bound, or ``functools.partial`` steps work too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..errors import PipelineAbortedError
from .async_policy import AsyncPolicy

if TYPE_CHECKING:
    from ..http.context.call_context import CallContext
    from ..http.request.request import Request
    from ..http.response.async_response import AsyncResponse
    from .context import PipelineContext


async def _resolve(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value


class _AsyncSansIORequestRunner(AsyncPolicy):
    """Wraps a request-side SansIO step (sync or async) in the async chain."""

    __slots__ = ("_step",)

    def __init__(
        self,
        step: Callable[[Request, CallContext], Request | None | Awaitable[Request | None]],
    ) -> None:
        self._step = step

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        transformed = await _resolve(self._step(request, ctx.call))
        if transformed is None:
            raise PipelineAbortedError(
                f"Pipeline step {self._step!r} returned None; aborting chain."
            )
        return await self.next.send(transformed, ctx)


class _AsyncSansIOResponseRunner(AsyncPolicy):
    """Wraps a response-side SansIO step (sync or async) in the async chain."""

    __slots__ = ("_step",)

    def __init__(
        self,
        step: Callable[
            [AsyncResponse, CallContext],
            AsyncResponse | None | Awaitable[AsyncResponse | None],
        ],
    ) -> None:
        self._step = step

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        response = await self.next.send(request, ctx)
        try:
            transformed: AsyncResponse | None = await _resolve(
                self._step(response, ctx.call),
            )
        except BaseException:
            await response.close()
            raise
        if transformed is None:
            await response.close()
            raise PipelineAbortedError(
                f"Pipeline step {self._step!r} returned None; aborting chain."
            )
        return transformed


__all__ = ["_AsyncSansIORequestRunner", "_AsyncSansIOResponseRunner"]
