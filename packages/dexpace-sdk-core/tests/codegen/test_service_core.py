# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Behavioral-parity suite for `ServiceCore` and `AsyncServiceCore`.

Each scenario — execute a simple op, execute a raw request, paginate, stream
events, and thread per-call options — is run identically against both executors
and asserts equivalent behavior. The sync and async twins share every
non-awaiting decision through the pure planners, so a divergence here means a
shell drifted.
"""

from __future__ import annotations

import pytest

from dexpace.sdk.core.codegen import AsyncServiceCore, Operation, OperationInput, ServiceCore
from dexpace.sdk.core.codegen._pure.options import AUTH_OPTION_KEY
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.http.sse.typed import SseSignal
from dexpace.sdk.core.pagination import CursorStrategy
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline
from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.context import PipelineContext
from dexpace.sdk.core.pipeline.policy import Policy
from dexpace.sdk.core.pipeline.stage import Stage

from .support import (
    AsyncOptionsProbe,
    AsyncScriptTransport,
    OptionsProbe,
    Pet,
    Reply,
    ScriptTransport,
)

_BASE = "https://api.example.com"


def _get_pet() -> Operation:
    return Operation(name="getPet", method=Method.GET, path="/pets/{id}")


def _list_pets() -> Operation:
    return Operation(name="listPets", method=Method.GET, path="/pets")


def _stream() -> Operation:
    return Operation(name="streamPets", method=Method.GET, path="/pets/events")


class _PassThroughAuth(Policy):
    """A stand-in auth policy passed as the per-call ``auth`` override.

    It is only parked in the options mapping, never wired into a pipeline, so
    its ``send`` is never called.
    """

    STAGE = Stage.AUTH
    __slots__ = ()

    def send(self, request: Request, ctx: PipelineContext) -> Response:  # pragma: no cover
        raise NotImplementedError


class _AsyncPassThroughAuth(AsyncPolicy):
    """Async twin of `_PassThroughAuth`."""

    STAGE = Stage.AUTH
    __slots__ = ()

    async def send(
        self, request: Request, ctx: PipelineContext
    ) -> AsyncResponse:  # pragma: no cover
        raise NotImplementedError


def _mapper(event: str, data: str) -> str | SseSignal:
    """Map an SSE event to its data, stopping on a ``[DONE]`` sentinel."""
    if data == "[DONE]":
        return SseSignal.DONE
    return data


_SSE_BODY = b"data: one\n\ndata: two\n\ndata: [DONE]\n\n"


# --------------------------------------------------------------------------- #
# execute a simple op — decode into a response_type (through the default stack) #
# --------------------------------------------------------------------------- #


def test_execute_decodes_response_type_sync() -> None:
    transport = ScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    with ServiceCore(base_url=_BASE, transport=transport) as core:
        pet = core.execute(_get_pet(), OperationInput(path_params={"id": "7"}), response_type=Pet)
    assert pet == Pet("Rex")
    assert str(transport.requests[0].url) == f"{_BASE}/pets/7"
    assert transport.close_calls == 1  # owned transport closed on context exit


async def test_execute_decodes_response_type_async() -> None:
    transport = AsyncScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    async with AsyncServiceCore(base_url=_BASE, transport=transport) as core:
        pet = await core.execute(
            _get_pet(), OperationInput(path_params={"id": "7"}), response_type=Pet
        )
    assert pet == Pet("Rex")
    assert str(transport.requests[0].url) == f"{_BASE}/pets/7"
    assert transport.aclose_calls == 1


# --------------------------------------------------------------------------- #
# execute an error op — the StatusErrorMap-mapped error is raised              #
# --------------------------------------------------------------------------- #


def test_execute_raises_mapped_error_sync() -> None:
    from dexpace.sdk.core.errors import HttpResponseError

    transport = ScriptTransport([Reply(Status.NOT_FOUND, b'{"message": "nope"}')])
    core = ServiceCore(base_url=_BASE, pipeline=Pipeline(transport))
    with pytest.raises(HttpResponseError):
        core.execute(_get_pet(), OperationInput(path_params={"id": "7"}), response_type=Pet)


async def test_execute_raises_mapped_error_async() -> None:
    from dexpace.sdk.core.errors import HttpResponseError

    transport = AsyncScriptTransport([Reply(Status.NOT_FOUND, b'{"message": "nope"}')])
    core = AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport))
    with pytest.raises(HttpResponseError):
        await core.execute(_get_pet(), OperationInput(path_params={"id": "7"}), response_type=Pet)


# --------------------------------------------------------------------------- #
# execute_request — a raw call returns the live response for the caller         #
# --------------------------------------------------------------------------- #


def test_execute_request_returns_raw_response_sync() -> None:
    transport = ScriptTransport([Reply(Status.OK, b"hello", headers=(("x-trace", "abc"),))])
    core = ServiceCore(base_url=_BASE, pipeline=Pipeline(transport))
    request = Request(method=Method.GET, url=Url.parse(f"{_BASE}/raw"))
    with core.execute_request(request) as response:
        assert response.status is Status.OK
        assert response.headers.get("x-trace") == "abc"
        assert response.body is not None
        assert response.body.string() == "hello"


async def test_execute_request_returns_raw_response_async() -> None:
    transport = AsyncScriptTransport([Reply(Status.OK, b"hello", headers=(("x-trace", "abc"),))])
    core = AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport))
    request = Request(method=Method.GET, url=Url.parse(f"{_BASE}/raw"))
    response = await core.execute_request(request)
    assert isinstance(response, AsyncResponse)
    async with response:
        assert response.status is Status.OK
        assert response.headers.get("x-trace") == "abc"
        assert response.body is not None
        assert await response.body.string() == "hello"


# --------------------------------------------------------------------------- #
# paginate — walk every page's items in order                                  #
# --------------------------------------------------------------------------- #

_PAGE_1 = b'{"data": [{"name": "a"}, {"name": "b"}], "next_cursor": "c2"}'
_PAGE_2 = b'{"data": [{"name": "c"}], "next_cursor": null}'


def _cursor_strategy() -> CursorStrategy[dict[str, str]]:
    return CursorStrategy(
        items_field="data", cursor_response_field="next_cursor", cursor_param="cursor"
    )


def test_paginate_walks_all_pages_sync() -> None:
    transport = ScriptTransport([Reply(Status.OK, _PAGE_1), Reply(Status.OK, _PAGE_2)])
    core = ServiceCore(base_url=_BASE, pipeline=Pipeline(transport))
    items = list(core.paginate(_list_pets(), OperationInput(), _cursor_strategy()))
    assert [item["name"] for item in items] == ["a", "b", "c"]
    assert len(transport.requests) == 2  # two pages, one exchange each


async def test_paginate_walks_all_pages_async() -> None:
    transport = AsyncScriptTransport([Reply(Status.OK, _PAGE_1), Reply(Status.OK, _PAGE_2)])
    core = AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport))
    items = [
        item async for item in core.paginate(_list_pets(), OperationInput(), _cursor_strategy())
    ]
    assert [item["name"] for item in items] == ["a", "b", "c"]
    assert len(transport.requests) == 2


# --------------------------------------------------------------------------- #
# events — stream mapped values, stopping on the mapper's DONE signal           #
# --------------------------------------------------------------------------- #


def test_events_streams_mapped_values_sync() -> None:
    transport = ScriptTransport([Reply(Status.OK, _SSE_BODY)])
    core = ServiceCore(base_url=_BASE, pipeline=Pipeline(transport))
    with core.events(_stream(), OperationInput(), _mapper) as stream:
        values = list(stream)
    assert values == ["one", "two"]


async def test_events_streams_mapped_values_async() -> None:
    transport = AsyncScriptTransport([Reply(Status.OK, _SSE_BODY)])
    core = AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport))
    async with core.events(_stream(), OperationInput(), _mapper) as stream:
        values = [value async for value in stream]
    assert values == ["one", "two"]


# --------------------------------------------------------------------------- #
# per-call options + auth threading                                            #
# --------------------------------------------------------------------------- #


def test_execute_threads_options_and_parks_auth_sync() -> None:
    probe = OptionsProbe()
    auth = _PassThroughAuth()
    transport = ScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = ServiceCore(base_url=_BASE, pipeline=Pipeline(transport, policies=[probe]))
    core.execute(
        _get_pet(),
        OperationInput(path_params={"id": "1"}),
        response_type=Pet,
        auth=auth,
        marker="xyz",
    )
    assert len(probe.seen) == 1
    assert probe.seen[0]["marker"] == "xyz"
    assert probe.seen[0][AUTH_OPTION_KEY] is auth


async def test_execute_threads_options_and_parks_auth_async() -> None:
    probe = AsyncOptionsProbe()
    auth = _AsyncPassThroughAuth()
    transport = AsyncScriptTransport([Reply(Status.OK, b'{"name": "Rex"}')])
    core = AsyncServiceCore(base_url=_BASE, pipeline=AsyncPipeline(transport, policies=[probe]))
    await core.execute(
        _get_pet(),
        OperationInput(path_params={"id": "1"}),
        response_type=Pet,
        auth=auth,
        marker="xyz",
    )
    assert len(probe.seen) == 1
    assert probe.seen[0]["marker"] == "xyz"
    assert probe.seen[0][AUTH_OPTION_KEY] is auth
