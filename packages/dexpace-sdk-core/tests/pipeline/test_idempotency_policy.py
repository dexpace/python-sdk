# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``IdempotencyPolicy`` and ``AsyncIdempotencyPolicy``."""

from __future__ import annotations

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
from dexpace.sdk.core.pipeline.policies.async_idempotency import AsyncIdempotencyPolicy
from dexpace.sdk.core.pipeline.policies.idempotency import IdempotencyPolicy
from dexpace.sdk.core.pipeline.stage import Stage

_HEADER = "Idempotency-Key"


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request(method: Method = Method.POST, *, key: str | None = None) -> Request:
    req = Request(method=method, url=Url.parse("https://api.example.com/v1"))
    if key is not None:
        req = req.with_header(_HEADER, key)
    return req


class _CountingFactory:
    """Deterministic key factory yielding ``key-1``, ``key-2``, ... per call."""

    __slots__ = ("_n",)

    def __init__(self) -> None:
        self._n = 0

    def __call__(self) -> str:
        self._n += 1
        return f"key-{self._n}"


class _RecordingClient(HttpClient):
    """Captures requests handed to the transport for assertion."""

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


def test_stage_runs_before_retry() -> None:
    assert IdempotencyPolicy.STAGE < Stage.RETRY
    assert AsyncIdempotencyPolicy.STAGE < Stage.RETRY


def test_key_added_to_post() -> None:
    client = _RecordingClient()
    with Pipeline(client, policies=[IdempotencyPolicy()]) as p:
        p.run(_request(Method.POST), DispatchContext(_instr("0" * 16 + "1")))
    assert client.calls[0].headers.get(_HEADER) is not None


@pytest.mark.parametrize("method", [Method.POST, Method.PUT, Method.PATCH])
def test_key_added_to_write_methods(method: Method) -> None:
    client = _RecordingClient()
    with Pipeline(client, policies=[IdempotencyPolicy()]) as p:
        p.run(_request(method), DispatchContext(_instr("0" * 16 + "2")))
    assert client.calls[0].headers.get(_HEADER) is not None


@pytest.mark.parametrize("method", [Method.GET, Method.DELETE, Method.HEAD, Method.OPTIONS])
def test_key_not_added_to_non_write_methods(method: Method) -> None:
    client = _RecordingClient()
    with Pipeline(client, policies=[IdempotencyPolicy()]) as p:
        p.run(_request(method), DispatchContext(_instr("0" * 16 + "3")))
    assert client.calls[0].headers.get(_HEADER) is None


def test_caller_set_key_preserved() -> None:
    client = _RecordingClient()
    with Pipeline(client, policies=[IdempotencyPolicy()]) as p:
        p.run(_request(Method.POST, key="caller-key"), DispatchContext(_instr("0" * 16 + "4")))
    assert client.calls[0].headers.get(_HEADER) == "caller-key"


def test_default_key_is_uuid4_shaped() -> None:
    from uuid import UUID

    client = _RecordingClient()
    with Pipeline(client, policies=[IdempotencyPolicy()]) as p:
        p.run(_request(Method.POST), DispatchContext(_instr("0" * 16 + "5")))
    key = client.calls[0].headers.get(_HEADER)
    assert key is not None
    parsed = UUID(key)  # raises ValueError if malformed
    assert parsed.version == 4


def test_existing_key_not_regenerated() -> None:
    """A request that already carries a key is forwarded untouched.

    This is the mechanism by which a key survives retries: the policy sits
    outside the retry wrapper, mints the key on the first pass, and on any
    re-send sees the header is present and leaves it alone.
    """
    factory = _CountingFactory()
    client = _RecordingClient()
    policy = IdempotencyPolicy(key_factory=factory)
    with Pipeline(client, policies=[policy]) as p:
        # First send mints key-1.
        first = p.run(_request(Method.POST), DispatchContext(_instr("0" * 16 + "6")))
        carried = first.request.headers.get(_HEADER)
        assert carried == "key-1"
        # Re-send the already-stamped request; the policy must not mint key-2.
        resent = _request(Method.POST, key=carried)
        p.run(resent, DispatchContext(_instr("0" * 16 + "7")))
    assert client.calls[1].headers.get(_HEADER) == "key-1"


def test_custom_methods_and_header() -> None:
    client = _RecordingClient()
    policy = IdempotencyPolicy(methods=[Method.DELETE], header="X-Idem")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(Method.DELETE), DispatchContext(_instr("0" * 16 + "8")))
        p.run(_request(Method.POST), DispatchContext(_instr("0" * 16 + "9")))
    assert client.calls[0].headers.get("X-Idem") is not None
    assert client.calls[1].headers.get("X-Idem") is None


async def test_async_key_added_to_post() -> None:
    client = _RecordingAsyncClient()
    async with AsyncPipeline(client, policies=[AsyncIdempotencyPolicy()]) as p:
        await p.run(_request(Method.POST), DispatchContext(_instr("0" * 16 + "a")))
    assert client.calls[0].headers.get(_HEADER) is not None


async def test_async_caller_set_key_preserved() -> None:
    client = _RecordingAsyncClient()
    async with AsyncPipeline(client, policies=[AsyncIdempotencyPolicy()]) as p:
        await p.run(
            _request(Method.POST, key="caller-key"),
            DispatchContext(_instr("0" * 16 + "b")),
        )
    assert client.calls[0].headers.get(_HEADER) == "caller-key"


async def test_async_get_not_stamped() -> None:
    client = _RecordingAsyncClient()
    async with AsyncPipeline(client, policies=[AsyncIdempotencyPolicy()]) as p:
        await p.run(_request(Method.GET), DispatchContext(_instr("0" * 16 + "c")))
    assert client.calls[0].headers.get(_HEADER) is None
