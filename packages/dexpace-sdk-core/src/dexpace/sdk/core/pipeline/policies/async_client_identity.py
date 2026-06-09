# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async twin of :class:`ClientIdentityPolicy`."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from ...http.common.http_header_name import USER_AGENT
from ..async_policy import AsyncPolicy
from ..stage import Stage
from .client_identity import default_user_agent

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.async_response import AsyncResponse
    from ..context import PipelineContext


class AsyncClientIdentityPolicy(AsyncPolicy):
    """Async variant of :class:`ClientIdentityPolicy`.

    Behaviour mirrors the sync twin: the ``User-Agent`` defaults to
    :func:`default_user_agent`, append-vs-replace is selectable, and the token
    is guaranteed non-blank. Building the value is synchronous, so :meth:`send`
    differs only in the ``await`` on the downstream call.

    Attributes:
        STAGE: Pinned to :attr:`Stage.POST_RETRY` at the type level so
            mis-slotting is caught by ``mypy``.
    """

    STAGE: ClassVar[Literal[Stage.POST_RETRY]] = Stage.POST_RETRY
    __slots__ = ("_replace", "_user_agent")

    def __init__(self, *, user_agent: str | None = None, replace: bool = False) -> None:
        """Build the policy.

        Args:
            user_agent: ``User-Agent`` token to stamp. ``None`` (the default)
                uses :func:`default_user_agent`. An empty or whitespace-only
                value is rejected so the header is never blank.
            replace: When ``True``, overwrite any caller-set ``User-Agent``.
                When ``False`` (the default), append after the caller's value.

        Raises:
            ValueError: If ``user_agent`` is provided but empty or whitespace.
        """
        resolved = default_user_agent() if user_agent is None else user_agent
        if not resolved.strip():
            raise ValueError("user_agent must be a non-empty token string")
        self._user_agent = resolved
        self._replace = replace

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        """Stamp ``request`` with the ``User-Agent`` header and dispatch.

        Args:
            request: Outgoing request.
            ctx: Pipeline context, forwarded unchanged.

        Returns:
            The response from the downstream chain.
        """
        existing = request.headers.get(USER_AGENT)
        if self._replace or not existing or not existing.strip():
            value = self._user_agent
        else:
            value = f"{existing} {self._user_agent}"
        return await self.next.send(request.with_header(USER_AGENT, value), ctx)


__all__ = ["AsyncClientIdentityPolicy"]
