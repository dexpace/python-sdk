# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Terminal Policy that hands the request to the configured ``HttpClient``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .policy import Policy

if TYPE_CHECKING:
    from ..client.http_client import HttpClient
    from ..http.request.request import Request
    from ..http.response.response import Response
    from .context import PipelineContext


class _TransportRunner(Policy):
    """Wraps an ``HttpClient`` as the terminal node of the policy chain.

    Has no ``.next`` — it is the bottom of the chain. As a side effect, the
    runner promotes the immutable telemetry context to an ``ExchangeContext``
    once the response is in hand so post-exchange observers (logging,
    tracing) can look up the latest snapshot via ``ContextStore``. The
    promotion records ``response.request`` — the per-hop request that actually
    produced the response, which differs from ``ctx.call.request`` after a
    redirect. The promotion is a snapshot update; ``ctx.call`` itself is not
    reassigned.
    """

    __slots__ = ("_client",)

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        response = self._client.execute(request)
        ctx.call.to_exchange_context(response)
        return response


__all__ = ["_TransportRunner"]
