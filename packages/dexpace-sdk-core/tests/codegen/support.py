# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Shared fixtures for the executor tests — scripted transports and a model.

Not a test module (no ``test_`` prefix), so pytest does not collect it; it is
imported by the executor test suites. It supplies sync/async scripted
transports that build fresh responses per call (so single-use bodies never leak
across calls), an ownership-tracking close ledger, an options-probe policy pair,
and a tiny decodable model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dexpace.sdk.core.http.common import Protocol
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.policy import Policy
from dexpace.sdk.core.pipeline.stage import Stage

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from dexpace.sdk.core.http.request.request import Request
    from dexpace.sdk.core.pipeline.context import PipelineContext


@dataclass(frozen=True, slots=True)
class Pet:
    """A tiny model the codec decodes ``{"name": ...}`` documents into."""

    name: str


@dataclass(frozen=True, slots=True)
class Reply:
    """A scripted reply: the status, an optional body, and optional headers."""

    status: Status
    body: bytes | None = None
    headers: tuple[tuple[str, str], ...] = ()


def _sync_response(reply: Reply, request: Request) -> Response:
    body = ResponseBody.from_bytes(reply.body) if reply.body is not None else None
    response = Response(request=request, protocol=Protocol.HTTP_1_1, status=reply.status, body=body)
    for name, value in reply.headers:
        response = response.with_header(name, value)
    return response


def _async_response(reply: Reply, request: Request) -> AsyncResponse:
    body = AsyncResponseBody.from_bytes(reply.body) if reply.body is not None else None
    response = AsyncResponse(
        request=request, protocol=Protocol.HTTP_1_1, status=reply.status, body=body
    )
    for name, value in reply.headers:
        response = response.with_header(name, value)
    return response


class ScriptTransport:
    """Sync ``HttpClient`` that replays a script, building a fresh response per call.

    The reply is chosen by request index and clamped to the last entry, so a
    stream that reconnects past the script re-serves the final reply rather than
    raising. ``close`` records its call count for the ownership matrix.
    """

    def __init__(self, script: Sequence[Reply]) -> None:
        self._script = list(script)
        self.requests: list[Request] = []
        self.close_calls = 0

    def execute(self, request: Request) -> Response:
        reply = self._script[min(len(self.requests), len(self._script) - 1)]
        self.requests.append(request)
        return _sync_response(reply, request)

    def close(self) -> None:
        self.close_calls += 1


class AsyncScriptTransport:
    """Async twin of `ScriptTransport` (records ``aclose`` calls)."""

    def __init__(self, script: Sequence[Reply]) -> None:
        self._script = list(script)
        self.requests: list[Request] = []
        self.aclose_calls = 0

    async def execute(self, request: Request) -> AsyncResponse:
        reply = self._script[min(len(self.requests), len(self._script) - 1)]
        self.requests.append(request)
        return _async_response(reply, request)

    async def aclose(self) -> None:
        self.aclose_calls += 1


class OptionsProbe(Policy):
    """Records the ``ctx.options`` mapping seen for each exchange."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("seen",)

    def __init__(self) -> None:
        self.seen: list[Mapping[str, Any]] = []

    def send(self, request: Request, ctx: PipelineContext) -> Response:
        self.seen.append(ctx.options)
        return self.next.send(request, ctx)


class AsyncOptionsProbe(AsyncPolicy):
    """Async twin of `OptionsProbe`."""

    STAGE = Stage.PRE_AUTH
    __slots__ = ("seen",)

    def __init__(self) -> None:
        self.seen: list[Mapping[str, Any]] = []

    async def send(self, request: Request, ctx: PipelineContext) -> AsyncResponse:
        self.seen.append(ctx.options)
        return await self.next.send(request, ctx)
