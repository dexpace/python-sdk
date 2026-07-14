# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Full redirect security battery run against ``AsyncRedirectPolicy``.

This is the battery referenced by the async-redirect gate decision (see
``docs/deviations.md``). It exercises every dimension the async twin must
satisfy before it may be wired into ``default_async_pipeline()`` by default:

- loop / max-hops / malformed-``Location`` handling — the current response is
  handed back unfollowed and *open* (never closed) so the caller can still read
  or close it;
- superseded (intermediate) responses closed before each hop, abandon paths
  leave the final response open;
- ``303`` rebuild strips the body and every ``Content-*`` header;
- cross-origin credential stripping;
- ``Location`` resolution;
- HTTPS→HTTP downgrade handling.

Dimensions the twin already satisfies are asserted directly. Dimensions it does
NOT yet satisfy are captured as ``xfail(strict=True)`` tests that encode the
*target* behaviour: they run today (proving the gap is real and grounding the
gate) and will flip to a hard failure the moment the shared resolution logic is
certified — a built-in reminder to remove the marker and un-gate the twin. The
twin delegates every per-hop decision to the wrapped sync ``RedirectPolicy``,
so certifying that shared logic closes these gaps on both sides at once.
"""

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
from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy


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
    headers: dict[str, str] | None = None,
) -> Request:
    req = Request(method=method, url=Url.parse(url), body=body)
    for name, value in (headers or {}).items():
        req = req.with_header(name, value)
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
    """One scripted transport response: status, Location, extra headers, body."""

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
    """Returns one scripted async response per call; records each request."""

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
        return await p.run(request, DispatchContext(_instr("0" * 15 + "3")))


# --------------------------------------------------------------------------- #
# Resource lifecycle: superseded responses closed; abandon paths leave open.  #
# --------------------------------------------------------------------------- #


class TestResourceLifecycle:
    async def test_superseded_responses_closed_before_each_hop(self) -> None:
        # Two intermediate 301 responses precede the terminal 200. Each
        # intermediate body must be closed as the policy moves off it; the
        # terminal response is handed back open for the caller to consume.
        b0 = _TrackingAsyncBody()
        b1 = _TrackingAsyncBody()
        final = _TrackingAsyncBody()
        client = _ScriptedAsyncClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a", body=b0),
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b", body=b1),
                _Hop(Status.OK, body=final),
            ],
        )
        response = await _run(client, AsyncRedirectPolicy(), _request())
        assert b0.closed
        assert b1.closed
        assert not final.closed
        assert response.status is Status.OK

    async def test_loop_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingAsyncBody()
        client = _ScriptedAsyncClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b"),
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a", body=final),
            ],
        )
        response = await _run(client, AsyncRedirectPolicy(), _request(url="https://example.com/a"))
        assert response.status is Status.MOVED_PERMANENTLY
        assert not final.closed

    async def test_max_hops_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingAsyncBody()
        client = _ScriptedAsyncClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a"),
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b", body=final),
                _Hop(Status.OK),
            ],
        )
        response = await _run(client, AsyncRedirectPolicy(max_hops=1), _request())
        assert response.status is Status.MOVED_PERMANENTLY
        assert not final.closed
        assert len(client.requests) == 2

    async def test_missing_location_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingAsyncBody()
        client = _ScriptedAsyncClient([_Hop(Status.MOVED_PERMANENTLY, None, body=final)])
        response = await _run(client, AsyncRedirectPolicy(), _request())
        assert response.status is Status.MOVED_PERMANENTLY
        assert not final.closed

    async def test_method_not_allowed_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingAsyncBody()
        client = _ScriptedAsyncClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new", body=final)],
        )
        # POST is not in the default allowed_methods, so a 301 is not followed.
        response = await _run(client, AsyncRedirectPolicy(), _request(method=Method.POST))
        assert response.status is Status.MOVED_PERMANENTLY
        assert not final.closed

    async def test_follow_303_false_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingAsyncBody()
        client = _ScriptedAsyncClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new", body=final)],
        )
        response = await _run(client, AsyncRedirectPolicy(follow_303=False), _request())
        assert response.status is Status.SEE_OTHER
        assert not final.closed

    async def test_malformed_location_returns_response_open_not_closed(self) -> None:
        # A malformed Location cannot resolve to a target. The policy must hand
        # the current 3xx response back unfollowed and OPEN — not raise and leak
        # the connection, and not close the body it is returning.
        final = _TrackingAsyncBody()
        client = _ScriptedAsyncClient([_Hop(Status.FOUND, "http://[malformed", body=final)])
        response = await _run(client, AsyncRedirectPolicy(), _request())
        assert response.status is Status.FOUND
        assert not final.closed
        assert len(client.requests) == 1

    async def test_non_replayable_body_closes_intermediate_then_raises(self) -> None:
        # A body-preserving reissue with a single-use body cannot be replayed.
        # The in-hand 3xx response is closed before the RuntimeError escapes so
        # the connection is not leaked (an error abandon, not a graceful one).
        intermediate = _TrackingAsyncBody()
        request = _request(method=Method.POST, body=RequestBody.from_iter(iter([b"chunk"])))
        client = _ScriptedAsyncClient(
            [_Hop(Status.TEMPORARY_REDIRECT, "https://example.com/new", body=intermediate)],
        )
        policy = AsyncRedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        with pytest.raises(RuntimeError):
            await _run(client, policy, request)
        assert intermediate.closed


# --------------------------------------------------------------------------- #
# Battery dimensions the async twin already satisfies.                        #
# --------------------------------------------------------------------------- #


class TestBatterySatisfied:
    async def test_303_rebuild_strips_body_and_content_headers(self) -> None:
        request = _request(
            method=Method.POST,
            body=RequestBody.from_bytes(b"payload"),
            headers={
                "Content-Type": "application/json",
                "Content-Length": "7",
                "Content-Encoding": "gzip",
            },
        )
        client = _ScriptedAsyncClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new"), _Hop(Status.OK)],
        )
        await _run(client, AsyncRedirectPolicy(), request)
        reissued = client.requests[1]
        assert reissued.method is Method.GET
        assert reissued.body is None
        assert "Content-Type" not in reissued.headers
        assert "Content-Length" not in reissued.headers
        assert "Content-Encoding" not in reissued.headers

    async def test_relative_location_resolved_against_base(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.MOVED_PERMANENTLY, "/elsewhere"), _Hop(Status.OK)],
        )
        await _run(client, AsyncRedirectPolicy(), _request(url="https://example.com/start"))
        assert str(client.requests[1].url) == "https://example.com/elsewhere"

    async def test_userinfo_in_location_dropped(self) -> None:
        client = _ScriptedAsyncClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://attacker:pw@example.com/new"),
                _Hop(Status.OK),
            ],
        )
        await _run(client, AsyncRedirectPolicy(), _request())
        assert client.requests[1].url.userinfo is None

    async def test_authorization_stripped_on_cross_origin(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.FOUND, "https://other.org/new"), _Hop(Status.OK)],
        )
        await _run(
            client, AsyncRedirectPolicy(), _request(headers={"Authorization": "Bearer secret"})
        )
        assert "Authorization" not in client.requests[1].headers

    async def test_authorization_kept_on_same_origin(self) -> None:
        # Same-origin hops keep the header: stripping it would de-authenticate
        # against the very service that issued the redirect.
        client = _ScriptedAsyncClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        await _run(
            client, AsyncRedirectPolicy(), _request(headers={"Authorization": "Bearer secret"})
        )
        assert client.requests[1].headers.get("Authorization") == "Bearer secret"


# --------------------------------------------------------------------------- #
# Shared resolution logic — no duplicated per-hop construction.               #
# --------------------------------------------------------------------------- #


class TestSharedResolutionLogic:
    def test_config_is_sync_policy_carrying_the_knobs(self) -> None:
        policy = AsyncRedirectPolicy(
            max_hops=3,
            follow_303=False,
            allowed_methods=frozenset({Method.GET}),
            strip_authorization=False,
        )
        cfg = policy.config
        assert isinstance(cfg, RedirectPolicy)
        assert cfg.max_hops == 3
        assert cfg.follow_303 is False
        assert cfg.allowed_methods == frozenset({Method.GET})
        assert cfg.strip_authorization is False

    async def test_reissue_matches_wrapped_sync_policy(self) -> None:
        # The request the async pipeline actually sends on a hop is byte-for-byte
        # what the wrapped sync RedirectPolicy computes for the same inputs —
        # proof the twin delegates rather than restating the resolution logic.
        policy = AsyncRedirectPolicy(allowed_methods=frozenset({Method.GET, Method.POST}))
        client = _ScriptedAsyncClient(
            [_Hop(Status.FOUND, "/next?a=b%20c"), _Hop(Status.OK)],
        )
        seed = _request(url="https://example.com/start")
        await _run(client, policy, seed)
        reissued = client.requests[1]
        direct = policy.config._build_next_request(seed, 302, "/next?a=b%20c")
        assert direct is not None
        assert str(reissued.url) == str(direct.url)
        assert reissued.method is direct.method


# --------------------------------------------------------------------------- #
# Battery dimensions the async twin does NOT yet satisfy (the gate reasons).  #
#                                                                             #
# These encode the target behaviour and fail today. `strict=True` turns an    #
# unexpected pass into a hard failure, so once the shared resolution logic is  #
# certified they flip red and force this file — and the default_async_pipeline #
# gate — to be revisited.                                                      #
# --------------------------------------------------------------------------- #

_PENDING = "pending full-battery certification of the shared redirect resolution logic"


class TestBatteryGaps:
    @pytest.mark.xfail(strict=True, reason=f"HTTPS->HTTP downgrade not default-denied; {_PENDING}")
    async def test_https_to_http_downgrade_default_denied(self) -> None:
        # A scheme downgrade should not be followed by default. The twin follows
        # it today (stripping credentials but still reissuing to the http host).
        client = _ScriptedAsyncClient(
            [_Hop(Status.FOUND, "http://example.com/new"), _Hop(Status.OK)],
        )
        await _run(client, AsyncRedirectPolicy(), _request(url="https://example.com/start"))
        assert len(client.requests) == 1  # not followed

    @pytest.mark.xfail(strict=True, reason=f"Cookie not stripped cross-origin; {_PENDING}")
    async def test_cross_origin_strips_cookie(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.FOUND, "https://other.org/new"), _Hop(Status.OK)],
        )
        await _run(client, AsyncRedirectPolicy(), _request(headers={"Cookie": "session=abc"}))
        assert "Cookie" not in client.requests[1].headers

    @pytest.mark.xfail(
        strict=True, reason=f"Proxy-Authorization not stripped cross-origin; {_PENDING}"
    )
    async def test_cross_origin_strips_proxy_authorization(self) -> None:
        client = _ScriptedAsyncClient(
            [_Hop(Status.FOUND, "https://other.org/new"), _Hop(Status.OK)],
        )
        await _run(
            client,
            AsyncRedirectPolicy(),
            _request(headers={"Proxy-Authorization": "Basic xyz"}),
        )
        assert "Proxy-Authorization" not in client.requests[1].headers

    async def test_location_resolution_is_wire_exact(self) -> None:
        # A %20 in the target query must survive verbatim. Fixed by the pure
        # RFC 3986 query codec (PY-M2-02), which closed this battery gap —
        # no longer xfail; see docs/deviations.md.
        client = _ScriptedAsyncClient(
            [_Hop(Status.FOUND, "https://example.com/p?x=%20y"), _Hop(Status.OK)],
        )
        await _run(client, AsyncRedirectPolicy(), _request())
        assert "%20" in str(client.requests[1].url)
