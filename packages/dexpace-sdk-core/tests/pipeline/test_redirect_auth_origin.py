# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Cross-cutting tests for redirect x auth origin handling.

These exercise the interaction the unit suites for ``RedirectPolicy`` and the
auth policies cannot reach in isolation: when the redirect policy (outermost)
reissues a request to a different origin and the auth policy (inner) sees that
reissued request, the credential must not follow the request to the foreign
host. They also pin that a caller-set ``Authorization`` header survives a
same-origin hop but is dropped on a cross-origin one.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.auth import (
    AccessTokenInfo,
    BasicAuthCredential,
    BasicAuthPolicy,
    BearerTokenPolicy,
    KeyCredential,
    KeyCredentialPolicy,
)
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, Status
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


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request(url: str = "https://api.example.com/start") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


class _Hop:
    """One scripted response: status plus an optional ``Location`` header."""

    __slots__ = ("location", "status")

    def __init__(self, status: Status, location: str | None = None) -> None:
        self.status = status
        self.location = location


class _ScriptedClient(HttpClient):
    """Returns one response per call and records every request seen."""

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
        )
        if hop.location is not None:
            response = response.with_header("Location", hop.location)
        return response


class _StaticCredential:
    """Token credential that mints a fresh long-lived token on every call."""

    def __init__(self) -> None:
        self.calls = 0

    def get_token_info(self, *scopes: str, options: object = None) -> AccessTokenInfo:
        del scopes, options
        self.calls += 1
        return AccessTokenInfo(token="abc", expires_on=int(time.time()) + 3600)

    def close(self) -> None:
        return None


def _run(client: _ScriptedClient, policies: Sequence[object], trace: str) -> Response:
    with Pipeline(client, policies=policies) as p:  # type: ignore[arg-type]
        return p.run(_request(), DispatchContext(_instr(trace)))


# --------------------------------------------------------------------------- #
# (a) BearerTokenPolicy + redirect: token must not reach a foreign host         #
# --------------------------------------------------------------------------- #


def test_bearer_token_not_reissued_to_cross_origin_redirect_target() -> None:
    # The redirect policy (outermost) 302s to a foreign host; the bearer policy
    # (inner) sees the reissued request but must NOT re-stamp the token onto it.
    client = _ScriptedClient(
        [
            _Hop(Status.FOUND, "https://attacker.example.net/loot"),
            _Hop(Status.OK),
        ],
    )
    cred = _StaticCredential()
    policies = [RedirectPolicy(), BearerTokenPolicy(cred, "scope-a")]
    response = _run(client, policies, "0" * 15 + "1")

    assert response.status is Status.OK
    # First hop (same origin) carried the token.
    assert client.requests[0].headers.get("authorization") == "Bearer abc"
    # Second hop crossed origin: no Authorization header reaches the foreign host.
    assert "authorization" not in client.requests[1].headers
    assert client.requests[1].url.host == "attacker.example.net"


def test_bearer_token_reissued_on_same_origin_redirect() -> None:
    # A same-origin hop still receives a token — the guard only withholds the
    # credential across origins, it does not break ordinary authed redirects.
    client = _ScriptedClient(
        [
            _Hop(Status.MOVED_PERMANENTLY, "https://api.example.com/start/"),
            _Hop(Status.OK),
        ],
    )
    cred = _StaticCredential()
    policies = [RedirectPolicy(), BearerTokenPolicy(cred, "scope-a")]
    response = _run(client, policies, "0" * 15 + "2")

    assert response.status is Status.OK
    assert client.requests[0].headers.get("authorization") == "Bearer abc"
    assert client.requests[1].headers.get("authorization") == "Bearer abc"


def test_bearer_token_not_acquired_for_cross_origin_only_redirect() -> None:
    # When the cross-origin hop is the only one that would need a token, the
    # credential is never invoked for it (it is invoked once for the same-origin
    # first hop, then withheld on the cross-origin reissue).
    client = _ScriptedClient(
        [
            _Hop(Status.FOUND, "https://other.example.org/next"),
            _Hop(Status.OK),
        ],
    )
    cred = _StaticCredential()
    policies = [RedirectPolicy(), BearerTokenPolicy(cred, "scope-a")]
    _run(client, policies, "0" * 15 + "3")

    # Exactly one token acquisition: the first (same-origin) hop.
    assert cred.calls == 1
    assert "authorization" not in client.requests[1].headers


# --------------------------------------------------------------------------- #
# (b) Caller-set Authorization: same-origin keeps it, cross-origin drops it     #
# --------------------------------------------------------------------------- #


def test_key_credential_not_reissued_cross_origin() -> None:
    client = _ScriptedClient(
        [
            _Hop(Status.FOUND, "https://attacker.example.net/loot"),
            _Hop(Status.OK),
        ],
    )
    policies = [RedirectPolicy(), KeyCredentialPolicy(KeyCredential("hunter2"), "X-API-Key")]
    _run(client, policies, "0" * 15 + "4")

    assert client.requests[0].headers.get("x-api-key") == "hunter2"
    assert "x-api-key" not in client.requests[1].headers


def test_key_credential_reissued_same_origin() -> None:
    client = _ScriptedClient(
        [
            _Hop(Status.MOVED_PERMANENTLY, "https://api.example.com/start/"),
            _Hop(Status.OK),
        ],
    )
    policies = [RedirectPolicy(), KeyCredentialPolicy(KeyCredential("hunter2"), "X-API-Key")]
    _run(client, policies, "0" * 15 + "5")

    assert client.requests[1].headers.get("x-api-key") == "hunter2"


def test_basic_auth_not_reissued_cross_origin() -> None:
    client = _ScriptedClient(
        [
            _Hop(Status.FOUND, "https://attacker.example.net/loot"),
            _Hop(Status.OK),
        ],
    )
    policies = [RedirectPolicy(), BasicAuthPolicy(BasicAuthCredential("user", "pass"))]
    _run(client, policies, "0" * 15 + "6")

    assert client.requests[0].headers.get("authorization") == "Basic dXNlcjpwYXNz"
    assert "authorization" not in client.requests[1].headers


def test_basic_auth_reissued_same_origin() -> None:
    client = _ScriptedClient(
        [
            _Hop(Status.MOVED_PERMANENTLY, "https://api.example.com/start/"),
            _Hop(Status.OK),
        ],
    )
    policies = [RedirectPolicy(), BasicAuthPolicy(BasicAuthCredential("user", "pass"))]
    _run(client, policies, "0" * 15 + "7")

    assert client.requests[1].headers.get("authorization") == "Basic dXNlcjpwYXNz"


def test_caller_authorization_kept_same_origin_dropped_cross_origin() -> None:
    # No auth policy at all: a caller-set Authorization header. The redirect
    # policy keeps it on a same-origin 301 and drops it on a cross-origin 302.
    same_origin = _ScriptedClient(
        [
            _Hop(Status.MOVED_PERMANENTLY, "https://api.example.com/start/"),
            _Hop(Status.OK),
        ],
    )
    req = _request().with_header("Authorization", "Bearer caller-set")
    with Pipeline(same_origin, policies=[RedirectPolicy()]) as p:
        p.run(req, DispatchContext(_instr("0" * 15 + "8")))
    assert same_origin.requests[1].headers.get("authorization") == "Bearer caller-set"

    cross_origin = _ScriptedClient(
        [
            _Hop(Status.FOUND, "https://attacker.example.net/loot"),
            _Hop(Status.OK),
        ],
    )
    with Pipeline(cross_origin, policies=[RedirectPolicy()]) as p:
        p.run(req, DispatchContext(_instr("0" * 15 + "9")))
    assert "authorization" not in cross_origin.requests[1].headers
