# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of `OperationTracingPolicy`."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from ..async_policy import AsyncPolicy
from ..stage import Stage
from .redirect import resolve_http_tracer

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.async_response import AsyncResponse
    from ..context import PipelineContext


class AsyncOperationTracingPolicy(AsyncPolicy):
    """Async variant of `OperationTracingPolicy`.

    Emits the per-operation ``HttpTracer`` lifecycle around the whole async
    call. Placed at `Stage.OPERATION`, outside the redirect and retry wrappers,
    so its single ``send`` brackets every hop and attempt: it emits
    ``operation_started`` before dispatching the chain and exactly one of
    ``operation_succeeded`` / ``operation_failed`` once the chain unwinds, so
    the operation outcome reflects what the caller observes rather than the
    result of the first attempt.

    This completes the async ``HttpTracer`` lifecycle. `AsyncRetryPolicy` and
    `AsyncRedirectPolicy` already emit the attempt-level events and
    ``request_url_resolved`` through the same per-operation tracer (resolved
    via ``resolve_http_tracer`` and cached in ``ctx.data``), so without this
    policy the async stack reports attempts but never the operation outcome.
    The tracer callbacks are synchronous, so the body matches the sync twin
    apart from the ``await`` on the downstream send.

    Disable per-call by setting ``ctx.options["tracing_enabled"] = False``.

    Attributes:
        STAGE: Pinned to `Stage.OPERATION` at the type level so mis-slotting is
            caught by ``mypy``.
    """

    STAGE: ClassVar[Literal[Stage.OPERATION]] = Stage.OPERATION
    __slots__ = ()

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        """Bracket the downstream chain with the per-operation lifecycle.

        Args:
            request: Outgoing request.
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        if not ctx.options.get("tracing_enabled", True):
            return await self.next.send(request, ctx)
        http_tracer = resolve_http_tracer(ctx)
        http_tracer.operation_started()
        try:
            response = await self.next.send(request, ctx)
        except BaseException as err:
            http_tracer.operation_failed(err)
            raise
        http_tracer.operation_succeeded()
        return response


__all__ = ["AsyncOperationTracingPolicy"]
