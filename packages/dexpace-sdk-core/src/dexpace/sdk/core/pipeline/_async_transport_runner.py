# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Terminal async policy that hands the request to the configured ``AsyncHttpClient``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .async_policy import AsyncPolicy

if TYPE_CHECKING:
    from ..client.async_http_client import AsyncHttpClient
    from ..http.request.request import Request
    from ..http.response.async_response import AsyncResponse
    from .context import PipelineContext


class _AsyncTransportRunner(AsyncPolicy):
    """Terminal node of the async policy chain.

    Like the sync transport runner, side-effects the promotion-chain
    context to record the exchange in the ``ContextStore`` after the
    response arrives, but does not reassign ``ctx.call``. The recorded
    ``ExchangeContext.request`` is ``response.request`` — the per-hop request
    that produced the response, which differs from ``ctx.call.request`` after
    a redirect.
    """

    __slots__ = ("_client",)

    def __init__(self, client: AsyncHttpClient) -> None:
        self._client = client

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        response = await self._client.execute(request)
        ctx.call.to_exchange_context(response)
        return response


__all__ = ["_AsyncTransportRunner"]
