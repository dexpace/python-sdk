# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncRedirectPolicy`` behaviour."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, AsyncResponseBody, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import AsyncPipeline
from dexpace.sdk.core.pipeline.policies.async_redirect import AsyncRedirectPolicy
from dexpace.sdk.core.pipeline.stage import Stage


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request(
    method: Method = Method.GET,
    url: str = "https://example.com/start",
    body: RequestBody | None = None,
    auth: str | None = None,
) -> Request:
    req = Request(method=method, url=Url.parse(url), body=body)
    if auth is not None:
        req = req.with_header("Authorization", auth)
    return req


class _TrackingAsyncBody(AsyncResponseBody):
    """Async response body that records whether ``close`` was called."""

    __slots__ = ("closed",)

    def __init__(self) -> None:
        self.closed = False

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        await self.close()
        return
        yield b""  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        self.closed = True


class _Hop:
    __slots__ = ("body", "extra_headers", "location", "status")

    def __init__(
        self,
        status: Status,
        location: str | None = None,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: AsyncResponseBody | None = None,
    ) -> None:
        self.status = status
        self.location = location
        self.extra_headers = extra_headers
        self.body = body


class _ScriptedAsyncClient(AsyncHttpClient):
    """Returns one async response per call; records each hop."""

    def __init__(self, hops: Sequence[_Hop]) -> None:
        self._hops = list(hops)
        self.requests: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        idx = len(self.requests)
        self.requests.append(request)
        hop = self._hops[idx]
        response = AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=hop.status,
            body=hop.body,
        )
        if hop.location is not None:
            response = response.with_header("Location", hop.location)
        for name, value in hop.extra_headers:
            response = response.with_header(name, value)
        return response


async def _run(
    client: _ScriptedAsyncClient,
    policy: AsyncRedirectPolicy,
    request: Request,
) -> AsyncResponse:
    async with AsyncPipeline(client, policies=[policy]) as p:
        return await p.run(request, DispatchContext(_instr("0" * 15 + "2")))


class TestStatusCodeMatrix:
    async def test_301_follows_get(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = AsyncRedirectPolicy()
        response = await _run(client, policy, _request())
        assert response.status is Status.OK
        assert client.requests[1].method is Method.GET
        assert str(client.requests[1].url) == "https://example.com/new"

    async def test_301_does_not_follow_post_when_not_allowed(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new")],
        )
        policy = AsyncRedirectPolicy()
        response = await _run(client, policy, _request(method=Method.POST))
        assert response.status is Status.MOVED_PERMANENTLY
        assert len(client.requests) == 1

    async def test_302_follows_with_original_method(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.FOUND, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = AsyncRedirectPolicy()
        response = await _run(client, policy, _request(method=Method.HEAD))
        assert response.status is Status.OK
        assert client.requests[1].method is Method.HEAD

    async def test_303_follow_303_true_reissues_as_get_and_drops_body(self) -> None:
        body = RequestBody.from_bytes(b"payload")
        request = _request(method=Method.POST, body=body).with_header(
            "Content-Type", "application/json"
        )
        client = _ScriptedAsyncClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = AsyncRedirectPolicy(follow_303=True)
        response = await _run(client, policy, request)
        assert response.status is Status.OK
        reissued = client.requests[1]
        assert reissued.method is Method.GET
        assert reissued.body is None
        assert "Content-Type" not in reissued.headers

    async def test_303_follow_303_false_does_not_follow(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new")],
        )
        policy = AsyncRedirectPolicy(follow_303=False)
        response = await _run(client, policy, _request())
        assert response.status is Status.SEE_OTHER

    async def test_307_follows_with_original_method_and_body(self) -> None:
        body = RequestBody.from_bytes(b"payload")
        request = _request(method=Method.POST, body=body)
        client = _ScriptedAsyncClient(
            [_Hop(Status.TEMPORARY_REDIRECT, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = AsyncRedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        response = await _run(client, policy, request)
        assert response.status is Status.OK
        reissued = client.requests[1]
        assert reissued.method is Method.POST
        assert reissued.body is not None
        assert b"".join(reissued.body.iter_bytes()) == b"payload"

    async def test_307_with_non_replayable_body_raises(self) -> None:
        body = RequestBody.from_iter(iter([b"chunk"]))
        request = _request(method=Method.POST, body=body)
        client = _ScriptedAsyncClient(
            [_Hop(Status.TEMPORARY_REDIRECT, "https://example.com/new")],
        )
        policy = AsyncRedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        with pytest.raises(RuntimeError):
            await _run(client, policy, request)

    async def test_307_non_replayable_body_closes_intermediate_response(self) -> None:
        # When the body-preserving rebuild raises on a single-use body, the
        # in-hand 307 response must be closed before the RuntimeError escapes.
        tracking_body = _TrackingAsyncBody()
        body = RequestBody.from_iter(iter([b"chunk"]))
        request = _request(method=Method.POST, body=body)
        client = _ScriptedAsyncClient(
            [
                _Hop(
                    Status.TEMPORARY_REDIRECT,
                    "https://example.com/new",
                    body=tracking_body,
                ),
            ],
        )
        policy = AsyncRedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        with pytest.raises(RuntimeError):
            await _run(client, policy, request)
        assert tracking_body.closed

    async def test_308_follows_with_original_method_and_body(self) -> None:
        body = RequestBody.from_bytes(b"payload")
        request = _request(method=Method.PUT, body=body)
        client = _ScriptedAsyncClient(
            [_Hop(Status.PERMANENT_REDIRECT, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = AsyncRedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.PUT}),
        )
        response = await _run(client, policy, request)
        assert response.status is Status.OK
        assert client.requests[1].method is Method.PUT

    async def test_non_3xx_pass_through(self) -> None:
        client = _ScriptedAsyncClient([_Hop(Status.OK)])
        policy = AsyncRedirectPolicy()
        response = await _run(client, policy, _request())
        assert response.status is Status.OK


class TestHopAndLoopGuards:
    async def test_max_hops_respected(self) -> None:
        hops = [
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a"),
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b"),
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/c"),
            _Hop(Status.OK),
        ]
        client = _ScriptedAsyncClient(hops)
        policy = AsyncRedirectPolicy(max_hops=2)
        response = await _run(client, policy, _request())
        assert len(client.requests) == 3
        assert response.status is Status.MOVED_PERMANENTLY

    async def test_loop_detection_returns_current_response(self) -> None:
        hops = [
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b"),
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a"),
        ]
        client = _ScriptedAsyncClient(hops)
        policy = AsyncRedirectPolicy()
        response = await _run(client, policy, _request(url="https://example.com/a"))
        assert len(client.requests) == 2
        assert response.status is Status.MOVED_PERMANENTLY


class TestSecurity:
    async def test_authorization_stripped_on_redirect(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = AsyncRedirectPolicy(strip_authorization=True)
        await _run(client, policy, _request(auth="Bearer secret"))
        assert "Authorization" not in client.requests[1].headers

    async def test_strip_authorization_false_preserves_header(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = AsyncRedirectPolicy(strip_authorization=False)
        await _run(client, policy, _request(auth="Bearer secret"))
        assert client.requests[1].headers.get("Authorization") == "Bearer secret"

    async def test_userinfo_in_location_dropped(self) -> None:
        client = _ScriptedAsyncClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://attacker:pw@example.com/new"),
                _Hop(Status.OK),
            ],
        )
        policy = AsyncRedirectPolicy()
        await _run(client, policy, _request())
        reissued = client.requests[1]
        assert reissued.url.userinfo is None
        assert reissued.url.host == "example.com"


class TestStageDeclaration:
    def test_stage_is_redirect(self) -> None:
        assert AsyncRedirectPolicy.STAGE is Stage.REDIRECT
