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

The credential-hygiene dimensions once committed as ``xfail(strict=True)``
executable specs are now certified directly on the shared core: ``Authorization``
is stripped before every reissue, an HTTPS→HTTP downgrade is default-denied (and
follow-on-opt-in), ``Cookie`` / ``Proxy-Authorization`` are stripped on a
cross-origin hop, cross-origin is judged against the seed origin, and a custom
predicate can fully override the follow decision. The async battery's twins pass
in lock-step because it delegates every per-hop decision to this same core.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest

from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.common.url import _origin
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
from dexpace.sdk.core.pipeline.policies.redirect import RedirectCondition, RedirectPolicy
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
    @pytest.mark.req("REDIR-22")
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

    @pytest.mark.req("REDIR-16", "REDIR-22")
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

    @pytest.mark.req("REDIR-17")
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

    @pytest.mark.req("REDIR-19")
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

    @pytest.mark.req("REDIR-22", "REDIR-6")
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

    @pytest.mark.req("REDIR-7", "XCUT-17")
    def test_authorization_stripped_on_same_origin(self) -> None:
        # Authorization is stripped before EVERY reissue — even same-origin.
        # Re-stamping a credential for a known origin is the auth layer's job.
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(headers={"Authorization": "Bearer secret"}))
        assert "Authorization" not in client.requests[1].headers


# --------------------------------------------------------------------------- #
# Golden Location-resolution corpus (REDIR-12): wire-exact end-to-end.        #
# --------------------------------------------------------------------------- #

_REDIRECT_VECTORS: tuple[RedirectVector, ...] = load_redirect()
_VECTOR_IDS: list[str] = [vector.id for vector in _REDIRECT_VECTORS]


class TestGoldenLocationResolution:
    @pytest.mark.req("REDIR-12", "REDIR-13")
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

    @pytest.mark.req("REDIR-12", "REDIR-13")
    def test_golden_percent_encoding_preserved(self) -> None:
        by_id = {vector.id: vector for vector in _REDIRECT_VECTORS}
        vector = by_id["preserve-percent-encoding"]
        client = _ScriptedClient(
            [_Hop(Status.FOUND, vector.location.decode()), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(url=vector.base.decode()))
        reissued = str(client.requests[1].url)
        assert "%2F" in reissued and "%20" in reissued  # never decoded/normalised

    @pytest.mark.req("REDIR-12", "REDIR-13")
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
        direct = policy._build_next_request(seed, 302, "/next?a=b%20c", _origin(seed.url))
        assert direct is not None
        assert str(reissued.url) == str(direct.url)
        assert reissued.method is direct.method


# --------------------------------------------------------------------------- #
# Credential-hygiene battery dimensions — now certified on the shared core.    #
#                                                                             #
# These once stood as ``xfail(strict=True)`` executable specs (HTTPS->HTTP    #
# downgrade denial, cross-origin Cookie / Proxy-Authorization stripping, and  #
# seed-origin cross-origin judgment). The shared resolution core now provides #
# them, so they assert directly — the async battery's twins flip in lock-step #
# because it delegates every per-hop decision to this same core.              #
# --------------------------------------------------------------------------- #


class TestCredentialHygieneBattery:
    @pytest.mark.req("REDIR-15", "XCUT-17")
    def test_https_to_http_downgrade_default_denied(self) -> None:
        # A scheme downgrade is not followed by default: the current response is
        # handed back unfollowed so the TLS guarantee cannot silently vanish.
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "http://example.com/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(url="https://example.com/start"))
        assert len(client.requests) == 1  # not followed

    @pytest.mark.req("REDIR-15", "XCUT-17")
    def test_https_to_http_downgrade_followed_when_opted_in(self) -> None:
        # allow_scheme_downgrade=True follows the downgrade (and logs a warning).
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "http://example.com/new"), _Hop(Status.OK)],
        )
        _run(
            client,
            RedirectPolicy(allow_scheme_downgrade=True),
            _request(url="https://example.com/start"),
        )
        assert len(client.requests) == 2  # followed
        assert str(client.requests[1].url) == "http://example.com/new"

    @pytest.mark.req("REDIR-9", "XCUT-17")
    def test_cross_origin_strips_cookie(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://other.org/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(headers={"Cookie": "session=abc"}))
        assert "Cookie" not in client.requests[1].headers

    @pytest.mark.req("REDIR-9", "XCUT-17")
    def test_cross_origin_strips_proxy_authorization(self) -> None:
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://other.org/new"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request(headers={"Proxy-Authorization": "Basic xyz"}))
        assert "Proxy-Authorization" not in client.requests[1].headers

    @pytest.mark.req("REDIR-9")
    def test_same_origin_keeps_cookie_and_proxy_authorization(self) -> None:
        # A same-origin hop keeps origin-scoped credentials — only Authorization
        # is stripped on a same-origin reissue.
        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        headers = {"Cookie": "session=abc", "Proxy-Authorization": "Basic xyz"}
        _run(client, RedirectPolicy(), _request(headers=headers))
        assert client.requests[1].headers.get("Cookie") == "session=abc"
        assert client.requests[1].headers.get("Proxy-Authorization") == "Basic xyz"

    @pytest.mark.req("REDIR-8", "XCUT-17")
    def test_cross_origin_judged_against_seed_not_previous_hop(self) -> None:
        # Seed a.example.com -> b.example.com (foreign) -> a.example.com again.
        # The final hop is same-origin relative to the PREVIOUS hop (b) but
        # cross-origin relative to the SEED (a). Because origin is judged against
        # the seed, the origin-scoped Cookie stays stripped for the whole chain
        # rather than being re-exposed when the chain lands back on a's host.
        client = _ScriptedClient(
            [
                _Hop(Status.FOUND, "https://b.example.com/step"),
                _Hop(Status.FOUND, "https://b.example.com/again"),
                _Hop(Status.OK),
            ],
        )
        _run(
            client,
            RedirectPolicy(),
            _request(url="https://a.example.com/start", headers={"Cookie": "session=abc"}),
        )
        # Hop 1 (a -> b): cross-origin, Cookie stripped.
        assert "Cookie" not in client.requests[1].headers
        # Hop 2 (b -> b): same-origin vs the previous hop, but the request no
        # longer carries a Cookie to leak; the seed-origin rule kept it stripped.
        assert "Cookie" not in client.requests[2].headers

    @pytest.mark.req("REDIR-20")
    def test_custom_predicate_overrides_follow_decision(self) -> None:
        # A predicate returning False stops a redirect the built-in matrix would
        # otherwise follow (a plain 301 for a GET).
        conditions: list[RedirectCondition] = []

        def never(condition: RedirectCondition) -> bool:
            conditions.append(condition)
            return False

        client = _ScriptedClient(
            [_Hop(Status.MOVED_PERMANENTLY, "https://example.com/new"), _Hop(Status.OK)],
        )
        response = _run(client, RedirectPolicy(should_redirect=never), _request())
        assert response.status is Status.MOVED_PERMANENTLY
        assert len(client.requests) == 1
        assert len(conditions) == 1
        # First decision: zero redirects followed, visited holds only the seed.
        assert conditions[0].redirect_count == 0
        assert conditions[0].visited_uris == ("https://example.com/start",)

    @pytest.mark.req("REDIR-20")
    def test_custom_predicate_follows_non_default_method(self) -> None:
        # A predicate returning True follows a POST 302 the default allow-list
        # would refuse — proof it fully overrides the built-in follow decision.
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://example.com/new"), _Hop(Status.OK)],
        )
        response = _run(
            client,
            RedirectPolicy(should_redirect=lambda _condition: True),
            _request(method=Method.POST),
        )
        assert response.status is Status.OK
        assert len(client.requests) == 2
        assert client.requests[1].method is Method.POST

    @pytest.mark.req("REDIR-20")
    def test_custom_predicate_snapshot_is_defensive_copy(self) -> None:
        # The visited-URI snapshot is an immutable tuple, so a predicate cannot
        # mutate the live cycle-detection state. It also includes the current
        # request's URI, and the count advances per followed hop.
        seen: list[tuple[int, tuple[str, ...]]] = []

        def record(condition: RedirectCondition) -> bool:
            assert isinstance(condition.visited_uris, tuple)  # immutable copy
            seen.append((condition.redirect_count, condition.visited_uris))
            return True

        client = _ScriptedClient(
            [
                _Hop(Status.FOUND, "https://example.com/a"),
                _Hop(Status.FOUND, "https://example.com/b"),
                _Hop(Status.OK),
            ],
        )
        _run(client, RedirectPolicy(should_redirect=record), _request())
        assert seen[0] == (0, ("https://example.com/start",))
        assert seen[1] == (1, ("https://example.com/start", "https://example.com/a"))

    def test_location_resolution_is_wire_exact(self) -> None:
        # A %20 in the target query must survive verbatim. Fixed by the pure
        # RFC 3986 query codec (PY-M2-02), which closed this battery gap —
        # no longer xfail; see docs/deviations.md.
        client = _ScriptedClient(
            [_Hop(Status.FOUND, "https://example.com/p?x=%20y"), _Hop(Status.OK)],
        )
        _run(client, RedirectPolicy(), _request())
        assert "%20" in str(client.requests[1].url)
