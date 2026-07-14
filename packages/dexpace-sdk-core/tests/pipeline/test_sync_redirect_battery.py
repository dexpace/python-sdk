# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Full redirect security battery run against the sync ``RedirectPolicy``.

Sync counterpart to ``test_async_redirect_battery.py``. The async twin
delegates every per-hop decision to a wrapped sync ``RedirectPolicy``, so both
share one resolution core; this file certifies that core directly on the sync
pillar, driving it through a real ``Pipeline`` with a close-tracking transport
double and the golden ``redirect`` corpus (``REDIR-12``).

Dimensions exercised:

- loop / max-hops / malformed-``Location`` / missing-``Location`` / disallowed
  method / ``follow_303=False`` all hand the current response back unfollowed
  and *open* (never closed) so the caller can still read or close it;
- superseded (intermediate) responses are closed before each subsequent hop;
  the abandon paths above leave the final response open;
- a body-preserving reissue with a single-use body closes the in-hand response
  before the ``RuntimeError`` escapes (an error abandon, not a graceful one);
- ``303`` rebuild strips the body and every ``Content-*`` header;
- cross-origin ``Authorization`` stripping (kept same-origin);
- ``userinfo`` in a redirected URL is dropped;
- ``Location`` resolution is wire-exact against every golden ``redirect`` vector
  — percent-encoding preserved verbatim and IPv6 ``[brackets]`` kept intact.

Dimensions the shared resolution core does NOT yet satisfy are captured as
``xfail(strict=True)`` executable specs, identical to the async battery: an
HTTPS→HTTP downgrade is not default-denied, and ``Cookie`` /
``Proxy-Authorization`` are not stripped on a cross-origin hop. These fail today
on both pillars (the core is shared) and are the reasons the async twin stays
gated out of ``default_async_pipeline()`` (see ``docs/deviations.md``). Because
``strict=True`` turns an unexpected pass into a hard failure, each flips red the
moment the shared core closes the gap — forcing this file, the async battery,
and the async gate to be revisited together.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import Response, ResponseBody, Status
from dexpace.sdk.core.instrumentation import (
    InstrumentationContext,
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)
from dexpace.sdk.core.instrumentation.noop import NOOP_SPAN
from dexpace.sdk.core.pipeline import Pipeline
from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy
from dexpace.sdk.tck.golden_corpus import RedirectVector, load_redirect


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


class _TrackingBody(ResponseBody):
    """Response body that records whether ``close`` was called."""

    __slots__ = ("closed",)

    def __init__(self) -> None:
        self.closed = False

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        self.close()
        return iter(())

    def close(self) -> None:
        self.closed = True


class _Hop:
    """One scripted transport response: status, Location, extra headers, body."""

    __slots__ = ("body", "extra_headers", "location", "status")

    def __init__(
        self,
        status: Status,
        location: str | None = None,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: ResponseBody | None = None,
    ) -> None:
        self.status = status
        self.location = location
        self.extra_headers = extra_headers
        self.body = body


class _ScriptedClient(HttpClient):
    """Returns one scripted response per call; records each request."""

    def __init__(self, hops: Sequence[_Hop]) -> None:
        self._hops = list(hops)
        self.requests: list[Request] = []

    def execute(self, request: Request) -> Response:
        idx = len(self.requests)
        self.requests.append(request)
        hop = self._hops[idx]
        response = Response(
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


def _run(client: _ScriptedClient, policy: RedirectPolicy, request: Request) -> Response:
    with Pipeline(client, policies=[policy]) as p:
        return p.run(request, DispatchContext(_instr("0" * 15 + "1")))


# --------------------------------------------------------------------------- #
# Resource lifecycle: superseded responses closed; abandon paths leave open.  #
# --------------------------------------------------------------------------- #


class TestResourceLifecycle:
    def test_superseded_responses_closed_before_each_hop(self) -> None:
        # Two intermediate 301 responses precede the terminal 200. Each
        # intermediate body must be closed as the policy moves off it; the
        # terminal response is handed back open for the caller to consume.
        b0 = _TrackingBody()
        b1 = _TrackingBody()
        final = _TrackingBody()
        client = _ScriptedClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a", body=b0),
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b", body=b1),
                _Hop(Status.OK, body=final),
            ],
        )
        response = _run(client, RedirectPolicy(), _request())
        assert b0.closed
        assert b1.closed
        assert not final.closed
        assert response.status is Status.OK

    def test_loop_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingBody()
        client = _ScriptedClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b"),
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a", body=final),
            ],
        )
        response = _run(client, RedirectPolicy(), _request(url="https://example.com/a"))
        assert response.status is Status.MOVED_PERMANENTLY
        assert not final.closed

    def test_max_hops_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingBody()
        client = _ScriptedClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a"),
                _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b", body=final),
                _Hop(Status.OK),
            ],
        )
        response = _run(client, RedirectPolicy(max_hops=1), _request())
        assert response.status is Status.MOVED_PERMANENTLY
        assert not final.closed
        assert len(client.requests) == 2

    def test_missing_location_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingBody()
        client = _ScriptedClient([_Hop(Status.MOVED_PERMANENTLY, None, body=final)])
        response = _run(client, RedirectPolicy(), _request())
        assert response.status is Status.MOVED_PERMANENTLY
        assert not final.closed

    def test_method_not_allowed_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingBody()
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new", body=final)],
        )
        # POST is not in the default allowed_methods, so a 301 is not followed.
        response = _run(client, RedirectPolicy(), _request(method=Method.POST))
        assert response.status is Status.MOVED_PERMANENTLY
        assert not final.closed

    def test_follow_303_false_abandon_leaves_final_response_open(self) -> None:
        final = _TrackingBody()
        client = _ScriptedClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new", body=final)],
        )
        response = _run(client, RedirectPolicy(follow_303=False), _request())
        assert response.status is Status.SEE_OTHER
        assert not final.closed

    def test_malformed_location_returns_response_open_not_closed(self) -> None:
        # A malformed Location cannot resolve to a target. The policy must hand
        # the current 3xx response back unfollowed and OPEN — not raise and leak
        # the connection, and not close the body it is returning.
        final = _TrackingBody()
        client = _ScriptedClient([_Hop(Status.FOUND, "http://[malformed", body=final)])
        response = _run(client, RedirectPolicy(), _request())
        assert response.status is Status.FOUND
        assert not final.closed
        assert len(client.requests) == 1

    def test_non_replayable_body_closes_intermediate_then_raises(self) -> None:
        # A body-preserving reissue with a single-use body cannot be replayed.
        # The in-hand 3xx response is closed before the RuntimeError escapes so
        # the connection is not leaked (an error abandon, not a graceful one).
        intermediate = _TrackingBody()
        request = _request(method=Method.POST, body=RequestBody.from_iter(iter([b"chunk"])))
        client = _ScriptedClient(
            [_Hop(Status.TEMPORARY_REDIRECT, "https://example.com/new", body=intermediate)],
        )
        policy = RedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        with pytest.raises(RuntimeError):
            _run(client, policy, request)
        assert intermediate.closed


# --------------------------------------------------------------------------- #
# Battery dimensions the shared resolution core already satisfies.            #
# --------------------------------------------------------------------------- #


class TestBatterySatisfied:
    def test_303_rebuild_strips_body_and_content_headers(self) -> None:
        request = _request(
            method=Method.POST,
            body=RequestBody.from_bytes(b"payload"),
            headers={
                "Content-Type": "application/json",
                "Content-Length": "7",
                "Content-Encoding": "gzip",
            },
        )
        client = _ScriptedClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), request)
        reissued = client.requests[1]
        assert reissued.method is Method.GET
        assert reissued.body is None
        assert "Content-Type" not in reissued.headers
        assert "Content-Length" not in reissued.headers
        assert "Content-Encoding" not in reissued.headers

    def test_relative_location_resolved_against_base(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "/elsewhere"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(url="https://example.com/start"))
        assert str(client.requests[1].url) == "https://example.com/elsewhere"

    def test_userinfo_in_location_dropped(self) -> None:
        client = _ScriptedClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://attacker:pw@example.com/new"),
                _Hop(Status.OK),
            ],
        )
        _run(client, RedirectPolicy(), _request())
        assert client.requests[1].url.userinfo is None

    def test_authorization_stripped_on_cross_origin(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://other.org/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(headers={"Authorization": "Bearer secret"}))
        assert "Authorization" not in client.requests[1].headers

    def test_authorization_kept_on_same_origin(self) -> None:
        # Same-origin hops keep the header: stripping it would de-authenticate
        # against the very service that issued the redirect.
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(headers={"Authorization": "Bearer secret"}))
        assert client.requests[1].headers.get("Authorization") == "Bearer secret"


# --------------------------------------------------------------------------- #
# Golden Location-resolution corpus (REDIR-12): wire-exact end-to-end.        #
# --------------------------------------------------------------------------- #

_REDIRECT_VECTORS: tuple[RedirectVector, ...] = load_redirect()
_VECTOR_IDS: list[str] = [vector.id for vector in _REDIRECT_VECTORS]


class TestGoldenLocationResolution:
    @pytest.mark.req("REDIR-12")
    @pytest.mark.parametrize("vector", _REDIRECT_VECTORS, ids=_VECTOR_IDS)
    def test_golden_location_resolves_wire_exact(self, vector: RedirectVector) -> None:
        # Drive each golden vector end-to-end: a GET seed at ``base`` gets a 302
        # to ``location``, and the URL the policy actually reissues must equal
        # the corpus's expected ``resolved`` byte-for-byte — no decode, no
        # re-encode, no normalisation.
        client = _ScriptedClient(
            [_Hop(Status.FOUND, vector.location.decode()), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(url=vector.base.decode()))
        assert len(client.requests) == 2
        assert str(client.requests[1].url) == vector.resolved.decode()

    @pytest.mark.req("REDIR-12")
    def test_golden_percent_encoding_preserved(self) -> None:
        by_id = {vector.id: vector for vector in _REDIRECT_VECTORS}
        vector = by_id["preserve-percent-encoding"]
        client = _ScriptedClient(
            [_Hop(Status.FOUND, vector.location.decode()), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(url=vector.base.decode()))
        reissued = str(client.requests[1].url)
        assert "%2F" in reissued and "%20" in reissued  # never decoded/normalised

    @pytest.mark.req("REDIR-12")
    @pytest.mark.parametrize(
        "vector_id", ["ipv6-brackets-kept-absolute", "ipv6-brackets-kept-relative"]
    )
    def test_golden_ipv6_brackets_kept(self, vector_id: str) -> None:
        vector = {v.id: v for v in _REDIRECT_VECTORS}[vector_id]
        client = _ScriptedClient(
            [_Hop(Status.FOUND, vector.location.decode()), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(url=vector.base.decode()))
        reissued = str(client.requests[1].url)
        assert "[" in reissued and "]" in reissued
        assert reissued == vector.resolved.decode()


# --------------------------------------------------------------------------- #
# Shared resolution logic — the send loop uses the per-hop helper.            #
# --------------------------------------------------------------------------- #


class TestSharedResolutionLogic:
    def test_send_loop_reissues_via_build_next_request(self) -> None:
        # The request the pipeline actually sends on a hop is byte-for-byte what
        # ``_build_next_request`` computes for the same inputs — proof the loop
        # delegates to the single per-hop resolution helper rather than
        # restating the logic inline (the same helper the async twin wraps).
        policy = RedirectPolicy(allowed_methods=frozenset({Method.GET, Method.POST}))
        client = _ScriptedClient([_Hop(Status.FOUND, "/next?a=b%20c"), _Hop(Status.OK)])
        seed = _request(url="https://example.com/start")
        _run(client, policy, seed)
        reissued = client.requests[1]
        direct = policy._build_next_request(seed, 302, "/next?a=b%20c")
        assert direct is not None
        assert str(reissued.url) == str(direct.url)
        assert reissued.method is direct.method


# --------------------------------------------------------------------------- #
# Battery dimensions the shared resolution core does NOT yet satisfy.         #
#                                                                             #
# These mirror ``test_async_redirect_battery.py::TestBatteryGaps`` exactly:   #
# the resolution core is shared, so the gaps are shared. `strict=True` turns  #
# an unexpected pass into a hard failure, so each flips red the moment the    #
# core closes the gap — forcing this file, the async battery, and the async   #
# `default_async_pipeline()` gate (docs/deviations.md) to be revisited        #
# together.                                                                   #
# --------------------------------------------------------------------------- #

_PENDING = "pending full-battery certification of the shared redirect resolution logic"


class TestBatteryGaps:
    @pytest.mark.xfail(strict=True, reason=f"HTTPS->HTTP downgrade not default-denied; {_PENDING}")
    def test_https_to_http_downgrade_default_denied(self) -> None:
        # A scheme downgrade should not be followed by default. The core follows
        # it today (stripping Authorization but still reissuing to the http host).
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "http://example.com/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(url="https://example.com/start"))
        assert len(client.requests) == 1  # not followed

    @pytest.mark.xfail(strict=True, reason=f"Cookie not stripped cross-origin; {_PENDING}")
    def test_cross_origin_strips_cookie(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://other.org/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(headers={"Cookie": "session=abc"}))
        assert "Cookie" not in client.requests[1].headers

    @pytest.mark.xfail(
        strict=True, reason=f"Proxy-Authorization not stripped cross-origin; {_PENDING}"
    )
    def test_cross_origin_strips_proxy_authorization(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://other.org/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(headers={"Proxy-Authorization": "Basic xyz"}))
        assert "Proxy-Authorization" not in client.requests[1].headers

    def test_location_resolution_is_wire_exact(self) -> None:
        # A %20 in the target query must survive verbatim. Fixed by the pure
        # RFC 3986 query codec (PY-M2-02), which closed this battery gap —
        # no longer xfail; see docs/deviations.md.
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://example.com/p?x=%20y"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request())
        assert "%20" in str(client.requests[1].url)
