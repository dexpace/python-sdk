# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``RedirectPolicy`` behaviour."""

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
    """One scripted transport response: status + Location header."""

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
    """Returns one response per call; records the request seen at each hop."""

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


class TestStatusCodeMatrix:
    @pytest.mark.req("REDIR-1", "REDIR-3")
    def test_301_follows_get(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy()
        response = _run(client, policy, _request())
        assert response.status is Status.OK
        assert len(client.requests) == 2
        assert client.requests[1].method is Method.GET
        assert str(client.requests[1].url) == "https://example.com/new"

    @pytest.mark.req("REDIR-3")
    def test_301_does_not_follow_post_when_not_allowed(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new")],
        )
        policy = RedirectPolicy()  # default allowed_methods = {GET, HEAD}
        response = _run(client, policy, _request(method=Method.POST))
        assert response.status is Status.MOVED_PERMANENTLY
        assert len(client.requests) == 1

    @pytest.mark.req("REDIR-3")
    def test_302_follows_with_original_method(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy()
        response = _run(client, policy, _request(method=Method.HEAD))
        assert response.status is Status.OK
        assert client.requests[1].method is Method.HEAD

    def test_303_follow_303_true_reissues_as_get_and_drops_body(self) -> None:
        body = RequestBody.from_bytes(b"payload")
        request = _request(method=Method.POST, body=body).with_header(
            "Content-Type", "application/json"
        )
        client = _ScriptedClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(follow_303=True)
        response = _run(client, policy, request)
        assert response.status is Status.OK
        reissued = client.requests[1]
        assert reissued.method is Method.GET
        assert reissued.body is None
        assert "Content-Type" not in reissued.headers
        # Content-Length must also be dropped if present.

    def test_303_follow_303_false_does_not_follow(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new")],
        )
        policy = RedirectPolicy(follow_303=False)
        response = _run(client, policy, _request())
        assert response.status is Status.SEE_OTHER
        assert len(client.requests) == 1

    @pytest.mark.req("REDIR-4")
    def test_307_follows_with_original_method_and_body(self) -> None:
        body = RequestBody.from_bytes(b"payload")
        request = _request(method=Method.POST, body=body).with_header(
            "Content-Type", "application/json"
        )
        client = _ScriptedClient(
            [_Hop(Status.TEMPORARY_REDIRECT, "https://example.com/new"), _Hop(Status.OK)],
        )
        # Allow POST so 307 may follow.
        policy = RedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        response = _run(client, policy, request)
        assert response.status is Status.OK
        reissued = client.requests[1]
        assert reissued.method is Method.POST
        assert reissued.body is not None
        assert b"".join(reissued.body.iter_bytes()) == b"payload"

    @pytest.mark.req("BODY-4", "REDIR-6")
    def test_307_with_non_replayable_body_raises(self) -> None:
        body = RequestBody.from_iter(iter([b"chunk"]))
        request = _request(method=Method.POST, body=body)
        client = _ScriptedClient(
            [_Hop(Status.TEMPORARY_REDIRECT, "https://example.com/new")],
        )
        policy = RedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        with pytest.raises(RuntimeError):
            _run(client, policy, request)

    def test_307_non_replayable_body_closes_intermediate_response(self) -> None:
        # The 3xx hop carries a body; when the body-preserving rebuild raises
        # because the request body is single-use, the in-hand 307 response must
        # be closed before the RuntimeError propagates — otherwise the
        # connection leaks.
        tracking_body = _TrackingBody()
        body = RequestBody.from_iter(iter([b"chunk"]))
        request = _request(method=Method.POST, body=body)
        client = _ScriptedClient(
            [
                _Hop(
                    Status.TEMPORARY_REDIRECT,
                    "https://example.com/new",
                    body=tracking_body,
                ),
            ],
        )
        policy = RedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.POST}),
        )
        with pytest.raises(RuntimeError):
            _run(client, policy, request)
        assert tracking_body.closed

    @pytest.mark.req("REDIR-4")
    def test_308_follows_with_original_method_and_body(self) -> None:
        body = RequestBody.from_bytes(b"payload")
        request = _request(method=Method.PUT, body=body)
        client = _ScriptedClient(
            [_Hop(Status.PERMANENT_REDIRECT, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(
            allowed_methods=frozenset({Method.GET, Method.HEAD, Method.PUT}),
        )
        response = _run(client, policy, request)
        assert response.status is Status.OK
        reissued = client.requests[1]
        assert reissued.method is Method.PUT
        assert reissued.body is not None
        assert b"".join(reissued.body.iter_bytes()) == b"payload"

    @pytest.mark.req("REDIR-1", "REDIR-21")
    def test_non_3xx_pass_through(self) -> None:
        client = _ScriptedClient([_Hop(Status.OK)])
        policy = RedirectPolicy()
        response = _run(client, policy, _request())
        assert response.status is Status.OK
        assert len(client.requests) == 1

    @pytest.mark.req("REDIR-1", "REDIR-2")
    def test_other_3xx_not_followed(self) -> None:
        # 304 NOT MODIFIED is 3xx but not in the redirect matrix.
        client = _ScriptedClient([_Hop(Status.NOT_MODIFIED)])
        policy = RedirectPolicy()
        response = _run(client, policy, _request())
        assert response.status is Status.NOT_MODIFIED
        assert len(client.requests) == 1


class TestHopAndLoopGuards:
    @pytest.mark.req("REDIR-17")
    def test_max_hops_respected(self) -> None:
        # Three redirects, but max_hops=2 — second redirect response is final.
        hops = [
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a"),
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b"),
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/c"),
            _Hop(Status.OK),
        ]
        client = _ScriptedClient(hops)
        policy = RedirectPolicy(max_hops=2)
        response = _run(client, policy, _request())
        # 1 initial + 2 follows = 3 requests; the third response is the final.
        assert len(client.requests) == 3
        assert response.status is Status.MOVED_PERMANENTLY

    @pytest.mark.req("REDIR-16")
    def test_loop_detection_returns_current_response(self) -> None:
        # /a -> /b -> /a (already visited; stop without raising)
        hops = [
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/b"),
            _Hop(Status.MOVED_PERMANENTLY, "https://example.com/a"),
        ]
        client = _ScriptedClient(hops)
        policy = RedirectPolicy()
        response = _run(client, policy, _request(url="https://example.com/a"))
        # 2 requests total; the second response (301 -> /a) is returned because
        # /a was already visited.
        assert len(client.requests) == 2
        assert response.status is Status.MOVED_PERMANENTLY


class TestSecurity:
    @pytest.mark.req("REDIR-7", "XCUT-17")
    def test_authorization_stripped_on_same_origin_redirect(self) -> None:
        # The Authorization header is stripped before EVERY reissue — even a
        # same-origin path change. Re-stamping a credential for a known origin
        # is the auth layer's job (a downstream auth policy re-applies it for a
        # same-origin reissue), not the redirect layer's.
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=True)
        response = _run(client, policy, _request(auth="Bearer secret"))
        assert response.is_success
        assert "Authorization" not in client.requests[1].headers
        # Original request kept its header.
        assert client.requests[0].headers.get("Authorization") == "Bearer secret"

    def test_authorization_stripped_on_cross_origin_redirect(self) -> None:
        # A reissue that crosses origin (different host) drops a caller-set
        # Authorization header so credentials never reach the foreign host.
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://other.example.org/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=True)
        response = _run(client, policy, _request(auth="Bearer secret"))
        assert response.is_success
        assert "Authorization" not in client.requests[1].headers

    @pytest.mark.req("REDIR-15", "XCUT-17")
    def test_scheme_downgrade_not_followed_by_default(self) -> None:
        # An HTTPS->HTTP downgrade is rejected by default: the hop is not
        # followed and the current 3xx response is handed back, so the TLS
        # guarantee cannot silently disappear across a redirect.
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "http://example.com/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=True)
        response = _run(
            client, policy, _request(url="https://example.com/start", auth="Bearer secret")
        )
        assert response.status is Status.FOUND
        assert len(client.requests) == 1

    @pytest.mark.req("REDIR-15", "XCUT-17")
    def test_scheme_downgrade_followed_when_opted_in_strips_credentials(self) -> None:
        # allow_scheme_downgrade=True follows the downgrade; credential
        # stripping still applies (the scheme change is cross-origin).
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "http://example.com/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=True, allow_scheme_downgrade=True)
        request = _request(url="https://example.com/start", auth="Bearer secret").with_header(
            "Cookie", "session=abc"
        )
        _run(client, policy, request)
        assert len(client.requests) == 2
        assert "Authorization" not in client.requests[1].headers
        assert "Cookie" not in client.requests[1].headers

    @pytest.mark.req("REDIR-8", "REDIR-9")
    def test_cookie_stripped_on_port_change(self) -> None:
        # Same scheme and host but a non-default port is a different origin, so
        # the origin-scoped Cookie is stripped.
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://example.com:8443/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=True)
        request = _request(url="https://example.com/start").with_header("Cookie", "session=abc")
        _run(client, policy, request)
        assert "Cookie" not in client.requests[1].headers

    @pytest.mark.req("REDIR-8")
    def test_cookie_kept_on_explicit_default_port_redirect(self) -> None:
        # An explicit :443 against an implied default https port is the same
        # origin (port normalized to the scheme default), so the origin-scoped
        # Cookie survives while Authorization is still stripped every reissue.
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com:443/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=True)
        request = _request(url="https://example.com/start", auth="Bearer secret").with_header(
            "Cookie", "session=abc"
        )
        _run(client, policy, request)
        assert client.requests[1].headers.get("Cookie") == "session=abc"
        assert "Authorization" not in client.requests[1].headers

    def test_authorization_stripped_on_cross_origin_303_get(self) -> None:
        # The 303 GET-rewrite path also honours cross-origin stripping.
        client = _ScriptedClient(
            [_Hop(Status.SEE_OTHER, "https://other.example.org/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=True)
        _run(client, policy, _request(method=Method.POST, auth="Bearer secret"))
        assert "Authorization" not in client.requests[1].headers

    @pytest.mark.req("REDIR-7", "XCUT-17")
    def test_authorization_stripped_on_same_origin_303_get(self) -> None:
        # The 303 GET rebuild strips Authorization too — even same-origin.
        client = _ScriptedClient(
            [_Hop(Status.SEE_OTHER, "https://example.com/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=True)
        _run(client, policy, _request(method=Method.POST, auth="Bearer secret"))
        assert "Authorization" not in client.requests[1].headers

    def test_strip_authorization_false_preserves_headers_cross_origin(self) -> None:
        # strip_authorization=False never strips any credential, even across
        # origins — the caller has audited every destination in the chain.
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://other.example.org/new"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy(strip_authorization=False)
        request = _request(auth="Bearer secret").with_header("Cookie", "session=abc")
        _run(client, policy, request)
        assert client.requests[1].headers.get("Authorization") == "Bearer secret"
        assert client.requests[1].headers.get("Cookie") == "session=abc"

    @pytest.mark.req("REDIR-12", "XCUT-17")
    def test_userinfo_in_location_dropped(self) -> None:
        client = _ScriptedClient(
            [
                _Hop(Status.MOVED_PERMANENTLY, "https://attacker:pw@example.com/new"),
                _Hop(Status.OK),
            ],
        )
        policy = RedirectPolicy()
        _run(client, policy, _request())
        reissued = client.requests[1]
        assert reissued.url.userinfo is None
        assert reissued.url.host == "example.com"
        assert reissued.url.path == "/new"

    @pytest.mark.req("REDIR-14")
    def test_relative_location_resolved(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "/elsewhere"), _Hop(Status.OK)],
        )
        policy = RedirectPolicy()
        _run(client, policy, _request(url="https://example.com/start"))
        assert str(client.requests[1].url) == "https://example.com/elsewhere"


class TestStageDeclaration:
    def test_stage_is_redirect(self) -> None:
        assert RedirectPolicy.STAGE is Stage.REDIRECT


class TestMissingLocation:
    @pytest.mark.req("REDIR-19")
    def test_redirect_without_location_returns_response(self) -> None:
        # 301 with no Location header — nothing to redirect to.
        client = _ScriptedClient([_Hop(Status.MOVED_PERMANENTLY)])
        policy = RedirectPolicy()
        response = _run(client, policy, _request())
        assert response.status is Status.MOVED_PERMANENTLY
        assert len(client.requests) == 1


class TestMalformedLocation:
    @pytest.mark.req("REDIR-18")
    def test_malformed_location_returns_response_open_not_closed(self) -> None:
        # A Location that cannot be resolved to an absolute URL must not raise
        # (leaking the connection). The policy returns the current 3xx response
        # unfollowed and open — the same disposition as loop / max-hops.
        tracking_body = _TrackingBody()
        client = _ScriptedClient([_Hop(Status.FOUND, "http://[malformed", body=tracking_body)])
        policy = RedirectPolicy()
        response = _run(client, policy, _request())
        assert response.status is Status.FOUND
        assert not tracking_body.closed
        assert len(client.requests) == 1
