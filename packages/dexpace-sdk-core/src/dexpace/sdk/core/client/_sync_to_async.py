# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Adapt a blocking ``HttpClient`` for use from an async pipeline.

``SyncToAsyncHttpClient`` presents a synchronous transport as an
``AsyncHttpClient``. Every blocking call is offloaded to a caller-supplied
``concurrent.futures.Executor`` so it never stalls the event loop. The executor
is mandatory: a default, implicitly-shared thread pool is exactly the kind of
easily-exhausted resource that causes hard-to-debug starvation in production,
so the bridge refuses to fabricate one and fails at construction instead.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..http.response.async_response import AsyncResponse
from ..http.response.async_response_body import AsyncResponseBody
from ..instrumentation.correlation import bind_to_context
from .async_http_client import AsyncHttpClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from concurrent.futures import Executor

    from ..http.common.media_type import MediaType
    from ..http.request.request import Request
    from ..http.response.response import Response
    from ..http.response.response_body import ResponseBody
    from .http_client import HttpClient

_CHUNK_SENTINEL = object()


class SyncToAsyncHttpClient(AsyncHttpClient):
    """Run a blocking ``HttpClient`` from async code via a required executor.

    The wrapped client's ``execute`` runs on ``executor`` (never the event
    loop), and the resulting ``Response`` is adapted to an ``AsyncResponse``
    whose body is streamed back through the same executor. The caller-supplied
    context (correlation ids and any other ``contextvars``) is captured at
    submission time so the worker observes it, not whatever a pooled thread
    happened to inherit.
    """

    __slots__ = ("_client", "_executor")

    def __init__(self, client: HttpClient, *, executor: Executor) -> None:
        """Wrap ``client``, offloading its blocking calls to ``executor``.

        Args:
            client: The blocking transport to adapt.
            executor: The executor that runs every blocking call. Required and
                keyword-only — the bridge never creates an implicit shared
                pool.

        Raises:
            TypeError: If ``executor`` is ``None``. Omitting it entirely raises
                ``TypeError`` for the missing keyword-only argument. Either way
                the failure is immediate, at construction.
        """
        if executor is None:
            raise TypeError("SyncToAsyncHttpClient requires an explicit executor")
        self._client = client
        self._executor = executor

    async def execute(self, request: Request) -> AsyncResponse:
        """Run the wrapped sync ``execute`` on the executor and adapt its result.

        Args:
            request: The request to send, threaded through unchanged.

        Returns:
            An ``AsyncResponse`` mirroring the sync ``Response``, with its body
            adapted to stream through the executor.
        """
        loop = asyncio.get_running_loop()
        # Capture the caller's contextvars now, at submission, so the worker
        # thread observes them rather than a bare pooled-thread context.
        bound = bind_to_context(lambda: self._client.execute(request))
        response = await loop.run_in_executor(self._executor, bound)
        return _to_async_response(response, loop, self._executor)


def _to_async_response(
    response: Response, loop: asyncio.AbstractEventLoop, executor: Executor
) -> AsyncResponse:
    """Adapt a sync ``Response`` to an ``AsyncResponse`` sharing the executor."""
    body = response.body
    async_body = None if body is None else _ExecutorResponseBody(body, loop, executor)
    return AsyncResponse(
        request=response.request,
        protocol=response.protocol,
        status=response.status,
        headers=response.headers,
        reason=response.reason,
        body=async_body,
    )


def _next_chunk(iterator: Iterator[bytes]) -> object:
    """Pull one chunk from a sync iterator, or the sentinel at exhaustion."""
    return next(iterator, _CHUNK_SENTINEL)


class _ExecutorResponseBody(AsyncResponseBody):
    """Adapt a blocking ``ResponseBody`` to async by reading it on the executor.

    Each blocking read and the final close run on the executor rather than the
    event loop. Single-use semantics come straight from the wrapped body: its
    ``iter_bytes`` claims the once-guard, so a second ``aiter_bytes`` raises.
    """

    __slots__ = ("_executor", "_inner", "_loop")

    def __init__(
        self, inner: ResponseBody, loop: asyncio.AbstractEventLoop, executor: Executor
    ) -> None:
        self._inner = inner
        self._loop = loop
        self._executor = executor

    def media_type(self) -> MediaType | None:
        return self._inner.media_type()

    def content_length(self) -> int:
        return self._inner.content_length()

    def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        # Claim the wrapped body's single-use guard eagerly (may raise), then
        # stream its chunks off the event loop.
        iterator = self._inner.iter_bytes(chunk_size)
        return self._aiter(iterator)

    async def _aiter(self, iterator: Iterator[bytes]) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._loop.run_in_executor(self._executor, _next_chunk, iterator)
            if chunk is _CHUNK_SENTINEL:
                return
            assert isinstance(chunk, bytes)
            yield chunk

    async def close(self) -> None:
        await self._loop.run_in_executor(self._executor, self._inner.close)


__all__ = ["SyncToAsyncHttpClient"]
