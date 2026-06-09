# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Shared pipeline-dispatch abstractions for paginators and SSE connections.

Both the paginator and the reconnecting SSE client accept either a pipeline
(run once per request with a fresh dispatch context) or a bare send-callable.
These structural Protocols and callable aliases capture that shape in one
place so neither consumer has to depend on the other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..http.context.dispatch_context import DispatchContext
    from ..http.request.request import Request
    from ..http.response.async_response import AsyncResponse
    from ..http.response.response import Response

#: A callable that sends one request through the pipeline and returns its
#: response. Built from a pipeline when one is given, or passed directly.
type SendSync = Callable[["Request"], "Response"]
type SendAsync = Callable[["Request"], Awaitable["AsyncResponse"]]


@runtime_checkable
class SyncPipelineLike(Protocol):
    """Structural view of a sync pipeline: just the ``run`` entry point."""

    def run(self, request: Request, dispatch: DispatchContext) -> Response: ...


@runtime_checkable
class AsyncPipelineLike(Protocol):
    """Structural view of an async pipeline: just its ``run`` coroutine."""

    async def run(self, request: Request, dispatch: DispatchContext) -> AsyncResponse: ...


__all__ = ["AsyncPipelineLike", "SendAsync", "SendSync", "SyncPipelineLike"]
