# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of `SetDatePolicy`."""

from __future__ import annotations

from email.utils import formatdate
from typing import TYPE_CHECKING, ClassVar, Literal

from ...util.clock import ASYNC_SYSTEM_CLOCK, AsyncClock
from ..async_policy import AsyncPolicy
from ..stage import Stage

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.async_response import AsyncResponse
    from ..context import PipelineContext


class AsyncSetDatePolicy(AsyncPolicy):
    """Async variant of `SetDatePolicy`.

    The clock reading itself is synchronous (``AsyncClock.now`` returns a
    plain float), so the body of `send` is identical to the sync
    twin apart from the ``await`` on the downstream send.

    The policy is placed at `Stage.POST_RETRY` so each retry
    attempt receives a fresh timestamp.

    Attributes:
        STAGE: Pinned to `Stage.POST_RETRY` at the type level so
            mis-slotting is caught by ``mypy``.
    """

    STAGE: ClassVar[Literal[Stage.POST_RETRY]] = Stage.POST_RETRY
    __slots__ = ("_clock",)

    def __init__(self, *, clock: AsyncClock = ASYNC_SYSTEM_CLOCK) -> None:
        """Build the policy.

        Args:
            clock: Source of wall-clock time. Defaults to the process-wide
                `ASYNC_SYSTEM_CLOCK`; tests substitute a deterministic
                fake.
        """
        self._clock = clock

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        """Stamp ``request`` with a fresh ``Date`` header and dispatch.

        Args:
            request: Outgoing request.
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        stamped = request.with_header("Date", formatdate(self._clock.now(), usegmt=True))
        return await self.next.send(stamped, ctx)


__all__ = ["AsyncSetDatePolicy"]
