"""Async twin of ``RedirectPolicy``.

Mirrors :class:`RedirectPolicy` exactly — same status-code matrix, same
credential stripping, same loop guard — but ``send`` is ``async`` and
operates on ``AsyncResponse``. The per-hop decision helpers are shared via
delegation to a wrapped sync ``RedirectPolicy`` instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from ...http.request.method import Method
from ..async_policy import AsyncPolicy
from ..stage import Stage
from .redirect import _REDIRECT_STATUSES, RedirectPolicy

if TYPE_CHECKING:
    from ...http.request.request import Request
    from ...http.response.async_response import AsyncResponse
    from ..context import PipelineContext


class AsyncRedirectPolicy(AsyncPolicy):
    """Async redirect policy.

    Reuses :class:`RedirectPolicy` for configuration and per-hop request
    construction (status-code matrix, credential stripping, body replay
    check). Only the dispatch loop is awaited — ``self.next.send`` is the
    async chain head.

    Attributes:
        config: The underlying sync ``RedirectPolicy`` carrying knobs and
            per-hop construction helpers.
    """

    STAGE: ClassVar[Literal[Stage.REDIRECT]] = Stage.REDIRECT

    __slots__ = ("config",)

    config: RedirectPolicy

    def __init__(
        self,
        *,
        max_hops: int = 10,
        follow_303: bool = True,
        allowed_methods: frozenset[Method] = frozenset({Method.GET, Method.HEAD}),
        strip_authorization: bool = True,
    ) -> None:
        self.config = RedirectPolicy(
            max_hops=max_hops,
            follow_303=follow_303,
            allowed_methods=allowed_methods,
            strip_authorization=strip_authorization,
        )

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        cfg = self.config
        visited: dict[str, None] = {str(request.url): None}
        hops = 0
        current_request = request
        while True:
            response = await self.next.send(current_request, ctx)
            if hops >= cfg.max_hops:
                return response
            status = int(response.status)
            if status not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("Location")
            if location is None or not location.strip():
                return response
            next_request = cfg._build_next_request(current_request, status, location)
            if next_request is None:
                return response
            next_key = str(next_request.url)
            if next_key in visited:
                return response
            visited[next_key] = None
            await response.close()
            current_request = next_request
            hops += 1


__all__ = ["AsyncRedirectPolicy"]
