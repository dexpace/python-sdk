# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of ``Pipeline``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from ._async_sansio_runner import _AsyncSansIORequestRunner, _AsyncSansIOResponseRunner
from ._async_transport_runner import _AsyncTransportRunner
from .async_policy import AsyncPolicy
from .context import PipelineContext

if TYPE_CHECKING:
    from ..client.async_http_client import AsyncHttpClient
    from ..http.context.call_context import CallContext
    from ..http.context.dispatch_context import DispatchContext
    from ..http.request.request import Request
    from ..http.response.async_response import AsyncResponse


#: Member of the ``policies`` list passed to ``AsyncPipeline``. Either a
#: full ``AsyncPolicy`` (with ``.next`` chaining), a sync SansIO callable,
#: or an async SansIO callable. Step callables may be on the request side
#: or the response side (tag with ``.side = "response"``).
type _AsyncStep = (
    AsyncPolicy
    | Callable[
        [Request, CallContext],
        Request | None | Awaitable[Request | None],
    ]
    | Callable[
        [AsyncResponse, CallContext],
        AsyncResponse | None | Awaitable[AsyncResponse | None],
    ]
)


class AsyncPipeline:
    """Composes an ordered sequence of async policies around an ``AsyncHttpClient``.

    Mirrors ``Pipeline`` exactly with ``async`` semantics. SansIO steps are
    auto-wrapped via the ``side`` attribute (``"request"`` / ``"response"``,
    default ``"request"``). The terminal node is an ``_AsyncTransportRunner``.

    Use as an async context manager so transport ``aclose`` (when defined)
    runs deterministically::

        async with AsyncPipeline(transport, policies=[retry, auth]) as p:
            response = await p.run(request, dispatch_ctx)
    """

    __slots__ = ("_chain", "transport")

    def __init__(
        self,
        transport: AsyncHttpClient,
        policies: Sequence[_AsyncStep] | None = None,
    ) -> None:
        self.transport = transport
        wrapped: list[AsyncPolicy] = [
            entry if isinstance(entry, AsyncPolicy) else _wrap_step(entry)
            for entry in (policies or [])
        ]
        for i, policy in enumerate(wrapped[:-1]):
            policy.next = wrapped[i + 1]
        terminal = _AsyncTransportRunner(transport)
        if wrapped:
            wrapped[-1].next = terminal
        self._chain: AsyncPolicy = wrapped[0] if wrapped else terminal

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        aclose = getattr(self.transport, "aclose", None)
        if callable(aclose):
            result = aclose()
            if isinstance(result, Awaitable):
                await result

    async def run(
        self,
        request: Request,
        dispatch: DispatchContext,
        **options: Any,
    ) -> AsyncResponse:
        request_ctx = dispatch.to_request_context(request)
        ctx = PipelineContext(call=request_ctx, options=dict(options))
        try:
            return await self._chain.send(request, ctx)
        finally:
            # Evict the call's ``ContextStore`` entry once the chain has fully
            # unwound — in-chain observers have already read the latest tier.
            # The exchange context shares this trace id, so a single close
            # clears both tiers and prevents unbounded growth across calls.
            request_ctx.close()


def _wrap_step(step: Any) -> AsyncPolicy:
    if not callable(step):
        raise TypeError(f"Pipeline step {step!r} is neither an AsyncPolicy nor a callable.")
    side = getattr(step, "side", "request")
    if side == "response":
        return _AsyncSansIOResponseRunner(step)
    return _AsyncSansIORequestRunner(step)


__all__ = ["AsyncPipeline"]
