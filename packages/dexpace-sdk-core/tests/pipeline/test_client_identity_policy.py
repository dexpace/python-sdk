# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``ClientIdentityPolicy`` and ``AsyncClientIdentityPolicy``."""

from __future__ import annotations

import re

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import AsyncPipeline, Pipeline
from dexpace.sdk.core.pipeline.policies.async_client_identity import AsyncClientIdentityPolicy
from dexpace.sdk.core.pipeline.policies.client_identity import (
    ClientIdentityPolicy,
    default_user_agent,
)

_UA = "User-Agent"
_DEFAULT_UA = re.compile(r"^dexpace-sdk/\S+ python/\d+\.\d+\.\d+$")


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request(*, user_agent: str | None = None) -> Request:
    req = Request(method=Method.GET, url=Url.parse("https://api.example.com/v1"))
    if user_agent is not None:
        req = req.with_header(_UA, user_agent)
    return req


class _RecordingClient(HttpClient):
    def __init__(self) -> None:
        self.calls: list[Request] = []

    def execute(self, request: Request) -> Response:
        self.calls.append(request)
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


class _RecordingAsyncClient(AsyncHttpClient):
    def __init__(self) -> None:
        self.calls: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        self.calls.append(request)
        return AsyncResponse(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


def test_default_user_agent_shape() -> None:
    assert _DEFAULT_UA.match(default_user_agent()) is not None


def test_default_user_agent_never_blank() -> None:
    ua = default_user_agent()
    assert ua.strip()
    assert "dexpace-sdk/" in ua


def test_stamps_default_user_agent() -> None:
    client = _RecordingClient()
    with Pipeline(client, policies=[ClientIdentityPolicy()]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
    ua = client.calls[0].headers.get(_UA)
    assert ua is not None
    assert _DEFAULT_UA.match(ua) is not None


def test_append_preserves_caller_value() -> None:
    client = _RecordingClient()
    policy = ClientIdentityPolicy(user_agent="my-token")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(user_agent="caller/1.0"), DispatchContext(_instr("0" * 16 + "2")))
    assert client.calls[0].headers.get(_UA) == "caller/1.0 my-token"


def test_replace_overwrites_caller_value() -> None:
    client = _RecordingClient()
    policy = ClientIdentityPolicy(user_agent="my-token", replace=True)
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(user_agent="caller/1.0"), DispatchContext(_instr("0" * 16 + "3")))
    assert client.calls[0].headers.get(_UA) == "my-token"


def test_append_with_no_caller_value_uses_token_alone() -> None:
    client = _RecordingClient()
    policy = ClientIdentityPolicy(user_agent="my-token")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
    assert client.calls[0].headers.get(_UA) == "my-token"


def test_blank_caller_value_replaced_not_appended() -> None:
    client = _RecordingClient()
    policy = ClientIdentityPolicy(user_agent="my-token")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(user_agent="   "), DispatchContext(_instr("0" * 16 + "5")))
    assert client.calls[0].headers.get(_UA) == "my-token"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_blank_token_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ClientIdentityPolicy(user_agent=bad)


async def test_async_stamps_default_user_agent() -> None:
    client = _RecordingAsyncClient()
    async with AsyncPipeline(client, policies=[AsyncClientIdentityPolicy()]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "6")))
    ua = client.calls[0].headers.get(_UA)
    assert ua is not None
    assert _DEFAULT_UA.match(ua) is not None


async def test_async_append_preserves_caller_value() -> None:
    client = _RecordingAsyncClient()
    policy = AsyncClientIdentityPolicy(user_agent="my-token")
    async with AsyncPipeline(client, policies=[policy]) as p:
        await p.run(_request(user_agent="caller/1.0"), DispatchContext(_instr("0" * 16 + "7")))
    assert client.calls[0].headers.get(_UA) == "caller/1.0 my-token"


async def test_async_replace_overwrites_caller_value() -> None:
    client = _RecordingAsyncClient()
    policy = AsyncClientIdentityPolicy(user_agent="my-token", replace=True)
    async with AsyncPipeline(client, policies=[policy]) as p:
        await p.run(_request(user_agent="caller/1.0"), DispatchContext(_instr("0" * 16 + "8")))
    assert client.calls[0].headers.get(_UA) == "my-token"


@pytest.mark.parametrize("bad", ["", "  "])
def test_async_blank_token_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        AsyncClientIdentityPolicy(user_agent=bad)
