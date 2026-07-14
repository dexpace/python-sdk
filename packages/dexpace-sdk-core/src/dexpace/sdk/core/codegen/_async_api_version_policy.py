# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Private async pipeline policy that stamps a constant api-version header.

Async twin of `_api_version_policy.ApiVersionPolicy`; see that module for the
rationale behind the `Stage.PRE_SEND` placement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from ..pipeline.async_policy import AsyncPolicy
from ..pipeline.stage import Stage

if TYPE_CHECKING:
    from ..http.request.request import Request
    from ..http.response.async_response import AsyncResponse
    from ..pipeline.context import PipelineContext

__all__ = ["AsyncApiVersionPolicy"]


class AsyncApiVersionPolicy(AsyncPolicy):
    """Async twin of `ApiVersionPolicy`.

    Attributes:
        STAGE: Pinned to `Stage.PRE_SEND` at the type level so mis-slotting
            is caught by ``mypy``.
    """

    STAGE: ClassVar[Literal[Stage.PRE_SEND]] = Stage.PRE_SEND
    __slots__ = ("_header", "_value")

    def __init__(self, header: str, value: str) -> None:
        """Build the policy.

        Args:
            header: The header name the api-version value is stamped under.
            value: The api-version value stamped on every request.
        """
        self._header = header
        self._value = value

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        """Stamp ``request`` with the configured api-version header and dispatch.

        Args:
            request: Outgoing request. A new request is returned.
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        return await self.next.send(request.with_header(self._header, self._value), ctx)
