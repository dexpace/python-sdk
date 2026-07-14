# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the built-in authentication policies."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.auth import (
    AccessTokenInfo,
    AsyncBearerTokenPolicy,
    AuthenticateChallenge,
    BasicAuthCredential,
    BasicAuthPolicy,
    BasicChallengeHandler,
    BearerTokenPolicy,
    DigestChallengeHandler,
    InMemoryTokenCache,
    KeyCredential,
    KeyCredentialPolicy,
)
from dexpace.sdk.core.http.common import Protocol, Url
from dexpace.sdk.core.http.context import DispatchContext
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import AsyncResponse, Response, Status
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.response_body import ResponseBody
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

from ..conftest import FakeClock


def _instr(trace: str) -> InstrumentationContext:
    return InstrumentationContext(
        trace_id_type=TraceIdType.W3C,
        trace_id=TraceId(trace),
        span_id=SpanId("0" * 16),
        span=NOOP_SPAN,
        trace_flags=TraceFlags.NOOP,
        trace_state=TraceState.NOOP,
    )


def _request(url: str = "https://api.example.com/") -> Request:
    return Request(method=Method.GET, url=Url.parse(url))


class _CapturingClient(HttpClient):
    """Captures the request and replies with a configurable status."""

    def __init__(self, *, status: Status = Status.OK, www_auth: bool = False) -> None:
        self.status = status
        self.www_auth = www_auth
        self.calls: list[Request] = []
        self._lock = threading.Lock()

    def execute(self, request: Request) -> Response:
        with self._lock:
            self.calls.append(request)
        headers: list[tuple[str, str]] = []
        if self.www_auth and self.status is Status.UNAUTHORIZED:
            headers.append(("WWW-Authenticate", 'Bearer realm="api"'))
        from dexpace.sdk.core.http.common import Headers

        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=self.status,
            headers=Headers(headers),
        )


@pytest.mark.req("AUTH-26")
def test_key_credential_policy_stamps_header() -> None:
    client = _CapturingClient()
    policy = KeyCredentialPolicy(KeyCredential("hunter2"), "X-API-Key")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
    assert client.calls[0].headers.get("x-api-key") == "hunter2"


@pytest.mark.req("AUTH-26")
def test_key_credential_policy_prefix() -> None:
    client = _CapturingClient()
    policy = KeyCredentialPolicy(KeyCredential("k"), "Authorization", prefix="SharedKey")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "2")))
    assert client.calls[0].headers.get("authorization") == "SharedKey k"


def test_basic_auth_policy_stamps_header() -> None:
    client = _CapturingClient()
    policy = BasicAuthPolicy(BasicAuthCredential("user", "pass"))
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "3")))
    assert client.calls[0].headers.get("authorization") == "Basic dXNlcjpwYXNz"


class _RedirectingClient(HttpClient):
    """302s the first request to ``location`` then replies 200.

    Records every request so a test can inspect which headers reached the
    foreign host on the reissued hop.
    """

    def __init__(self, location: str) -> None:
        self._location = location
        self.calls: list[Request] = []

    def execute(self, request: Request) -> Response:
        from dexpace.sdk.core.http.common import Headers

        self.calls.append(request)
        if len(self.calls) == 1:
            return Response(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=Status.FOUND,
                headers=Headers([("Location", self._location)]),
            )
        return Response(request=request, protocol=Protocol.HTTP_1_1, status=Status.OK)


@pytest.mark.req("AUTH-29")
def test_key_credential_policy_withholds_credential_cross_origin() -> None:
    from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy

    client = _RedirectingClient("https://attacker.example.net/loot")
    policy = KeyCredentialPolicy(KeyCredential("hunter2"), "X-API-Key")
    with Pipeline(client, policies=[RedirectPolicy(), policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 15 + "30")))
    assert client.calls[0].headers.get("x-api-key") == "hunter2"
    assert "x-api-key" not in client.calls[1].headers


def test_basic_auth_policy_withholds_credential_cross_origin() -> None:
    from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy

    client = _RedirectingClient("https://attacker.example.net/loot")
    policy = BasicAuthPolicy(BasicAuthCredential("user", "pass"))
    with Pipeline(client, policies=[RedirectPolicy(), policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 15 + "31")))
    assert client.calls[0].headers.get("authorization") == "Basic dXNlcjpwYXNz"
    assert "authorization" not in client.calls[1].headers


class _StaticCredential:
    """Minimal TokenCredential — returns the same token unless explicitly told."""

    def __init__(
        self,
        token: str = "abc",
        expires_in: int = 3600,
        clock: FakeClock | None = None,
    ) -> None:
        self.calls = 0
        self.token = token
        self.expires_in = expires_in
        self._clock = clock

    def get_token_info(
        self,
        *scopes: str,
        options: object = None,
    ) -> AccessTokenInfo:
        del scopes, options
        self.calls += 1
        now = self._clock.now() if self._clock is not None else time.time()
        return AccessTokenInfo(
            token=self.token,
            expires_on=int(now) + self.expires_in,
        )

    def close(self) -> None:
        return None


@pytest.mark.req("AUTH-34")
def test_bearer_token_policy_stamps_header() -> None:
    client = _CapturingClient()
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
    assert client.calls[0].headers.get("authorization") == "Bearer abc"


@pytest.mark.req("AUTH-14")
def test_bearer_token_policy_caches_token() -> None:
    client = _CapturingClient()
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "5")))
        p.run(_request(), DispatchContext(_instr("0" * 16 + "6")))
    assert cred.calls == 1


def test_bearer_policy_refreshes_when_clock_advances_past_expiry() -> None:
    """Advancing the injected clock past ``expires_on`` triggers a re-fetch."""
    client = _CapturingClient()
    clock = FakeClock(start=1_000.0)
    # 1h-lived token; the policy's default 5-min leeway means it refreshes
    # once the clock crosses (expires_on - 300). Advance well past expiry.
    cred = _StaticCredential(expires_in=3600, clock=clock)
    policy = BearerTokenPolicy(cred, "scope-a", clock=clock)
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "a")))
        assert cred.calls == 1
        clock.advance(3600)
        p.run(_request(), DispatchContext(_instr("0" * 16 + "b")))
    assert cred.calls == 2


@pytest.mark.req("XCUT-16")
def test_bearer_token_policy_enforces_https() -> None:
    client = _CapturingClient()
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p, pytest.raises(ServiceRequestError):
        p.run(
            _request("http://insecure.example.com/"),
            DispatchContext(_instr("0" * 16 + "7")),
        )


@pytest.mark.req("AUTH-33")
def test_bearer_token_policy_surfaces_401_without_challenge() -> None:
    """A 401 without ``WWW-Authenticate`` is surfaced unchanged, hook untouched."""
    client = _CapturingClient(status=Status.UNAUTHORIZED)
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p:
        response = p.run(_request(), DispatchContext(_instr("0" * 16 + "8")))
    assert response.status is Status.UNAUTHORIZED
    # No challenge header: no re-issue, no extra token acquisition.
    assert len(client.calls) == 1
    assert cred.calls == 1


@pytest.mark.req("AUTH-30")
def test_bearer_token_policy_on_challenge_hook() -> None:
    """Subclass that authorizes one replay; the still-401 result is surfaced."""

    client = _CapturingClient(status=Status.UNAUTHORIZED, www_auth=True)
    cred = _StaticCredential()

    class _Retrying(BearerTokenPolicy):
        def on_challenge(self, request: Request, response: Response) -> bool:
            return True

    policy = _Retrying(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p:
        # Server keeps responding 401; after one replay the response surfaces.
        response = p.run(_request(), DispatchContext(_instr("0" * 16 + "9")))
    assert response.status is Status.UNAUTHORIZED
    # Two attempts: initial + one re-issue after challenge, then no loop.
    assert len(client.calls) == 2


@pytest.mark.req("AUTH-29")
def test_bearer_token_policy_withholds_token_cross_origin() -> None:
    """A redirect to a foreign host must not receive the bearer token."""
    from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy

    client = _RedirectingClient("https://attacker.example.net/loot")
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[RedirectPolicy(), policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 15 + "32")))
    assert client.calls[0].headers.get("authorization") == "Bearer abc"
    assert "authorization" not in client.calls[1].headers
    # The foreign hop neither acquired nor refreshed a token.
    assert cred.calls == 1


@pytest.mark.req("AUTH-29", "XCUT-16")
def test_bearer_token_policy_skips_https_enforcement_cross_origin() -> None:
    """A cross-origin reissue to http:// forwards unchanged, no HTTPS error."""
    from dexpace.sdk.core.pipeline.policies.redirect import RedirectPolicy

    # http:// target is cross-origin (scheme + host differ); the bearer policy
    # must forward it unchanged rather than raising the HTTPS-only error.
    client = _RedirectingClient("http://other.example.org/next")
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[RedirectPolicy(), policy]) as p:
        response = p.run(_request(), DispatchContext(_instr("0" * 15 + "33")))
    assert response.status is Status.OK
    assert "authorization" not in client.calls[1].headers


class _SlowCredential:
    """TokenCredential whose token fetch is slow — exercises concurrent refresh."""

    def __init__(self, delay: float = 0.05, clock: FakeClock | None = None) -> None:
        self.calls = 0
        self._delay = delay
        self._lock = threading.Lock()
        self._clock = clock

    def get_token_info(
        self,
        *scopes: str,
        options: object = None,
    ) -> AccessTokenInfo:
        del scopes, options
        with self._lock:
            self.calls += 1
        time.sleep(self._delay)
        now = self._clock.now() if self._clock is not None else time.time()
        return AccessTokenInfo(token="abc", expires_on=int(now) + 3600)

    def close(self) -> None:
        return None


@pytest.mark.req("XCUT-12")
def test_bearer_token_policy_serializes_concurrent_refresh() -> None:
    """Concurrent sync sends must issue exactly one token fetch."""

    client = _CapturingClient()
    cred = _SlowCredential()
    policy = BearerTokenPolicy(cred, "scope-a")

    trace_ids = [f"{i:032x}" for i in range(1, 9)]

    # A single pipeline (and thus a single policy instance, whose lock and
    # cache serialize the refresh) is shared across the worker threads. The
    # pipeline run is concurrency-safe; reusing one policy across separately
    # constructed pipelines is not — each Policy is owned by one pipeline.
    with Pipeline(client, policies=[policy]) as p:

        def _send(trace: str) -> None:
            p.run(_request(), DispatchContext(_instr(trace)))

        threads = [threading.Thread(target=_send, args=(t,)) for t in trace_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert cred.calls == 1


@pytest.mark.req("AUTH-34", "XCUT-11", "XCUT-12")
def test_bearer_token_policy_single_flight_under_thread_pool() -> None:
    """The sync pillar collapses N concurrent misses into one fetch (pool)."""

    client = _CapturingClient()
    cred = _SlowCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    trace_ids = [f"{i:032x}" for i in range(200, 208)]

    with Pipeline(client, policies=[policy]) as p:

        def _send(trace: str) -> None:
            p.run(_request(), DispatchContext(_instr(trace)))

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_send, t) for t in trace_ids]
            for future in futures:
                future.result(timeout=5)

    assert cred.calls == 1


class _SlowAsyncCredential:
    """AsyncTokenCredential whose token fetch is slow — for asyncio concurrency."""

    def __init__(self, delay: float = 0.05, clock: FakeClock | None = None) -> None:
        self.calls = 0
        self._delay = delay
        self._clock = clock

    async def get_token_info(
        self,
        *scopes: str,
        options: object = None,
    ) -> AccessTokenInfo:
        del scopes, options
        self.calls += 1
        await asyncio.sleep(self._delay)
        now = self._clock.now() if self._clock is not None else time.time()
        return AccessTokenInfo(token="abc", expires_on=int(now) + 3600)

    async def close(self) -> None:
        return None


class _CapturingAsyncClient(AsyncHttpClient):
    """Async twin of ``_CapturingClient``."""

    def __init__(
        self,
        *,
        status: Status = Status.OK,
        www_auth: bool = False,
    ) -> None:
        self.status = status
        self.www_auth = www_auth
        self.calls: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        from dexpace.sdk.core.http.common import Headers

        self.calls.append(request)
        header_pairs: list[tuple[str, str]] = []
        if self.www_auth and self.status is Status.UNAUTHORIZED:
            header_pairs.append(("WWW-Authenticate", 'Bearer realm="api"'))
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=self.status,
            headers=Headers(header_pairs),
        )


async def test_async_bearer_token_policy_serializes_concurrent_refresh() -> None:
    """Concurrent async sends must issue exactly one token fetch."""

    client = _CapturingAsyncClient()
    cred = _SlowAsyncCredential()
    policy = AsyncBearerTokenPolicy(cred, "scope-a")
    trace_ids = [f"{i:032x}" for i in range(100, 108)]

    async with AsyncPipeline(client, policies=[policy]) as p:
        await asyncio.gather(*(p.run(_request(), DispatchContext(_instr(t))) for t in trace_ids))

    assert cred.calls == 1


class _ScriptedClient(HttpClient):
    """Captures requests and replies with a scripted sequence of responses.

    Each call consumes the next ``(status, www_authenticate)`` entry from the
    queue. The final entry is reused if the queue runs dry.
    """

    def __init__(self, script: list[tuple[Status, str | None]]) -> None:
        if not script:
            raise ValueError("script must not be empty")
        self._script = script
        self.calls: list[Request] = []
        self._index = 0

    def execute(self, request: Request) -> Response:
        self.calls.append(request)
        idx = min(self._index, len(self._script) - 1)
        self._index += 1
        status, www_auth = self._script[idx]
        from dexpace.sdk.core.http.common import Headers

        header_pairs: list[tuple[str, str]] = []
        if www_auth is not None:
            header_pairs.append(("WWW-Authenticate", www_auth))
        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=status,
            headers=Headers(header_pairs),
        )


def test_challenge_handler_wires_digest_authentication() -> None:
    """Digest handler plugged into BearerTokenPolicy negotiates a 401 Digest."""

    digest_challenge = (
        'Digest realm="testrealm@host.com", '
        'qop="auth", '
        'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41", '
        "algorithm=MD5"
    )
    client = _ScriptedClient(
        [
            (Status.UNAUTHORIZED, digest_challenge),
            (Status.OK, None),
        ]
    )
    cred = _StaticCredential()
    handler = DigestChallengeHandler(
        "Mufasa",
        "Circle Of Life",
        cnonce_factory=lambda: "0a4f113b",
    )
    policy = BearerTokenPolicy(cred, "scope-a", challenge_handler=handler)
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "c")))

    assert len(client.calls) == 2
    auth = client.calls[1].headers.get("authorization")
    assert auth is not None
    assert auth.startswith("Digest ")
    assert 'username="Mufasa"' in auth
    assert 'realm="testrealm@host.com"' in auth
    assert "algorithm=MD5" in auth


def test_challenge_handler_can_handle_false_falls_through() -> None:
    """Handler that does not recognise the challenge falls back to on_challenge."""

    class _NoopHandler:
        def __init__(self) -> None:
            self.can_handle_calls = 0
            self.handle_calls = 0

        def can_handle(self, challenges: list[AuthenticateChallenge]) -> bool:
            del challenges
            self.can_handle_calls += 1
            return False

        def handle(
            self,
            method: Method,
            url: Url,
            challenges: list[AuthenticateChallenge],
            *,
            is_proxy: bool,
        ) -> tuple[str, str] | None:
            del method, url, challenges, is_proxy
            self.handle_calls += 1
            return None

    client = _CapturingClient(status=Status.UNAUTHORIZED, www_auth=True)
    cred = _StaticCredential()
    handler = _NoopHandler()

    on_challenge_calls = 0

    class _Spy(BearerTokenPolicy):
        def on_challenge(self, request: Request, response: Response) -> bool:
            nonlocal on_challenge_calls
            on_challenge_calls += 1
            return False

    policy = _Spy(cred, "scope-a", challenge_handler=handler)
    with Pipeline(client, policies=[policy]) as p:
        response = p.run(_request(), DispatchContext(_instr("0" * 16 + "d")))

    assert response.status is Status.UNAUTHORIZED
    assert handler.can_handle_calls == 1
    assert handler.handle_calls == 0
    assert on_challenge_calls == 1


class _StaticAsyncCredential:
    """Minimal AsyncTokenCredential — returns the same token unless told otherwise."""

    def __init__(
        self,
        token: str = "abc",
        expires_in: int = 3600,
        clock: FakeClock | None = None,
    ) -> None:
        self.calls = 0
        self.token = token
        self.expires_in = expires_in
        self._clock = clock

    async def get_token_info(
        self,
        *scopes: str,
        options: object = None,
    ) -> AccessTokenInfo:
        del scopes, options
        self.calls += 1
        now = self._clock.now() if self._clock is not None else time.time()
        return AccessTokenInfo(
            token=self.token,
            expires_on=int(now) + self.expires_in,
        )

    async def close(self) -> None:
        return None


class _ScriptedAsyncClient(AsyncHttpClient):
    """Async twin of ``_ScriptedClient``.

    Each call consumes the next ``(status, header_name, header_value)`` entry
    from the script. The final entry is reused if the script runs dry.
    """

    def __init__(self, script: list[tuple[Status, str | None, str | None]]) -> None:
        if not script:
            raise ValueError("script must not be empty")
        self._script = script
        self.calls: list[Request] = []
        self._index = 0

    async def execute(self, request: Request) -> AsyncResponse:
        from dexpace.sdk.core.http.common import Headers

        self.calls.append(request)
        idx = min(self._index, len(self._script) - 1)
        self._index += 1
        status, header_name, header_value = self._script[idx]
        header_pairs: list[tuple[str, str]] = []
        if header_name is not None and header_value is not None:
            header_pairs.append((header_name, header_value))
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=status,
            headers=Headers(header_pairs),
        )


async def test_async_challenge_handler_wires_digest_authentication() -> None:
    """Digest handler plugged into AsyncBearerTokenPolicy negotiates a 401 Digest."""

    digest_challenge = (
        'Digest realm="testrealm@host.com", '
        'qop="auth", '
        'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41", '
        "algorithm=MD5"
    )
    client = _ScriptedAsyncClient(
        [
            (Status.UNAUTHORIZED, "WWW-Authenticate", digest_challenge),
            (Status.OK, None, None),
        ]
    )
    cred = _StaticAsyncCredential()
    handler = DigestChallengeHandler(
        "Mufasa",
        "Circle Of Life",
        cnonce_factory=lambda: "0a4f113b",
    )
    policy = AsyncBearerTokenPolicy(cred, "scope-a", challenge_handler=handler)
    async with AsyncPipeline(client, policies=[policy]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 16 + "e")))

    assert len(client.calls) == 2
    auth = client.calls[1].headers.get("authorization")
    assert auth is not None
    assert auth.startswith("Digest ")
    assert 'username="Mufasa"' in auth
    assert 'realm="testrealm@host.com"' in auth
    assert "algorithm=MD5" in auth


async def test_async_challenge_handler_handles_407_proxy() -> None:
    """A 407 with Proxy-Authenticate is satisfied by a Basic handler."""

    client = _ScriptedAsyncClient(
        [
            (Status.PROXY_AUTHENTICATION_REQUIRED, "Proxy-Authenticate", 'Basic realm="proxy"'),
            (Status.OK, None, None),
        ]
    )
    cred = _StaticAsyncCredential()
    handler = BasicChallengeHandler("user", "pass")
    policy = AsyncBearerTokenPolicy(cred, "scope-a", challenge_handler=handler)
    async with AsyncPipeline(client, policies=[policy]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 15 + "10")))

    assert len(client.calls) == 2
    assert client.calls[1].headers.get("proxy-authorization") == "Basic dXNlcjpwYXNz"


async def test_async_407_without_handler_surfaces_response() -> None:
    """A 407 with no challenge_handler returns the proxy response unchanged."""

    client = _ScriptedAsyncClient(
        [(Status.PROXY_AUTHENTICATION_REQUIRED, "Proxy-Authenticate", 'Basic realm="proxy"')]
    )
    cred = _StaticAsyncCredential()
    policy = AsyncBearerTokenPolicy(cred, "scope-a")
    async with AsyncPipeline(client, policies=[policy]) as p:
        response = await p.run(_request(), DispatchContext(_instr("0" * 15 + "11")))

    assert int(response.status) == 407
    assert len(client.calls) == 1


async def test_async_407_does_not_invalidate_cached_token() -> None:
    """A 407 leaves the cached origin token untouched (only 401 invalidates)."""

    client = _ScriptedAsyncClient(
        [
            (Status.PROXY_AUTHENTICATION_REQUIRED, "Proxy-Authenticate", 'Basic realm="proxy"'),
            (Status.OK, None, None),
        ]
    )
    cred = _StaticAsyncCredential()
    handler = BasicChallengeHandler("user", "pass")
    policy = AsyncBearerTokenPolicy(cred, "scope-a", challenge_handler=handler)
    async with AsyncPipeline(client, policies=[policy]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 15 + "12")))
        await p.run(_request(), DispatchContext(_instr("0" * 15 + "13")))

    # Token fetched once; the 407 must not have evicted it.
    assert cred.calls == 1


async def test_async_challenge_handler_can_handle_false_falls_through() -> None:
    """Async handler that declines the challenge falls back to on_challenge."""

    class _NoopHandler:
        def __init__(self) -> None:
            self.can_handle_calls = 0
            self.handle_calls = 0

        def can_handle(self, challenges: list[AuthenticateChallenge]) -> bool:
            del challenges
            self.can_handle_calls += 1
            return False

        def handle(
            self,
            method: Method,
            url: Url,
            challenges: list[AuthenticateChallenge],
            *,
            is_proxy: bool,
        ) -> tuple[str, str] | None:
            del method, url, challenges, is_proxy
            self.handle_calls += 1
            return None

    client = _CapturingAsyncClient(status=Status.UNAUTHORIZED, www_auth=True)
    cred = _StaticAsyncCredential()
    handler = _NoopHandler()

    on_challenge_calls = 0

    class _Spy(AsyncBearerTokenPolicy):
        async def on_challenge(self, request: Request, response: AsyncResponse) -> bool:
            nonlocal on_challenge_calls
            on_challenge_calls += 1
            return False

    policy = _Spy(cred, "scope-a", challenge_handler=handler)
    async with AsyncPipeline(client, policies=[policy]) as p:
        response = await p.run(_request(), DispatchContext(_instr("0" * 15 + "14")))

    assert response.status is Status.UNAUTHORIZED
    assert handler.can_handle_calls == 1
    assert handler.handle_calls == 0
    assert on_challenge_calls == 1


# --------------------------------------------------------------------------- #
# A discarded 401/407 response is closed before the request is re-issued       #
# --------------------------------------------------------------------------- #

_DIGEST_CHALLENGE = (
    'Digest realm="testrealm@host.com", '
    'qop="auth", '
    'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
    'opaque="5ccc069c403ebaf9f0171e9517f40e41", '
    "algorithm=MD5"
)


class _RecordingResponseBody(ResponseBody):
    """Sync response body that records whether ``close`` was called."""

    def __init__(self) -> None:
        self.closed = False

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield b""

    def close(self) -> None:
        self.closed = True


class _AsyncRecordingResponseBody(AsyncResponseBody):
    """Async response body that records whether ``close`` was awaited."""

    def __init__(self) -> None:
        self.closed = False

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        yield b""

    async def close(self) -> None:
        self.closed = True


class _BodyScriptedClient(HttpClient):
    """Scripted client whose responses each carry an inspectable recording body."""

    def __init__(self, script: list[tuple[Status, str | None]]) -> None:
        self._script = script
        self._index = 0
        self.bodies: list[_RecordingResponseBody] = []

    def execute(self, request: Request) -> Response:
        from dexpace.sdk.core.http.common import Headers

        idx = min(self._index, len(self._script) - 1)
        self._index += 1
        status, www_auth = self._script[idx]
        pairs = [("WWW-Authenticate", www_auth)] if www_auth is not None else []
        body = _RecordingResponseBody()
        self.bodies.append(body)
        return Response(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=status,
            headers=Headers(pairs),
            body=body,
        )


class _AsyncBodyScriptedClient(AsyncHttpClient):
    """Async twin of ``_BodyScriptedClient``."""

    def __init__(self, script: list[tuple[Status, str | None]]) -> None:
        self._script = script
        self._index = 0
        self.bodies: list[_AsyncRecordingResponseBody] = []

    async def execute(self, request: Request) -> AsyncResponse:
        from dexpace.sdk.core.http.common import Headers

        idx = min(self._index, len(self._script) - 1)
        self._index += 1
        status, www_auth = self._script[idx]
        pairs = [("WWW-Authenticate", www_auth)] if www_auth is not None else []
        body = _AsyncRecordingResponseBody()
        self.bodies.append(body)
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=status,
            headers=Headers(pairs),
            body=body,
        )


def test_challenge_handler_closes_discarded_401_response() -> None:
    """The 401 whose body the handler retries past is released before re-issue."""
    client = _BodyScriptedClient([(Status.UNAUTHORIZED, _DIGEST_CHALLENGE), (Status.OK, None)])
    handler = DigestChallengeHandler("Mufasa", "Circle Of Life", cnonce_factory=lambda: "0a4f113b")
    policy = BearerTokenPolicy(_StaticCredential(), "scope-a", challenge_handler=handler)
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 15 + "2a")))

    assert client.bodies[0].closed, "discarded 401 response leaked: body was not closed"
    assert not client.bodies[1].closed, "final response handed to caller must stay open"


@pytest.mark.req("AUTH-30")
def test_on_challenge_closes_discarded_401_response() -> None:
    """The on_challenge retry path also releases the rejected 401."""
    client = _BodyScriptedClient([(Status.UNAUTHORIZED, 'Bearer realm="x"'), (Status.OK, None)])

    class _Retrying(BearerTokenPolicy):
        def on_challenge(self, request: Request, response: Response) -> bool:
            del request, response
            return True

    policy = _Retrying(_StaticCredential(), "scope-a")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 15 + "2b")))

    assert client.bodies[0].closed, "discarded 401 response leaked: body was not closed"
    assert not client.bodies[1].closed, "final response handed to caller must stay open"


async def test_async_challenge_handler_closes_discarded_401_response() -> None:
    """Async handler retry releases the rejected 401 before re-issue."""
    client = _AsyncBodyScriptedClient([(Status.UNAUTHORIZED, _DIGEST_CHALLENGE), (Status.OK, None)])
    handler = DigestChallengeHandler("Mufasa", "Circle Of Life", cnonce_factory=lambda: "0a4f113b")
    policy = AsyncBearerTokenPolicy(_StaticAsyncCredential(), "scope-a", challenge_handler=handler)
    async with AsyncPipeline(client, policies=[policy]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 15 + "2c")))

    assert client.bodies[0].closed, "discarded 401 response leaked: body was not closed"
    assert not client.bodies[1].closed, "final response handed to caller must stay open"


async def test_async_on_challenge_closes_discarded_401_response() -> None:
    """Async on_challenge retry releases the rejected 401."""
    client = _AsyncBodyScriptedClient(
        [(Status.UNAUTHORIZED, 'Bearer realm="x"'), (Status.OK, None)]
    )

    class _Retrying(AsyncBearerTokenPolicy):
        async def on_challenge(self, request: Request, response: AsyncResponse) -> bool:
            del request, response
            return True

    policy = _Retrying(_StaticAsyncCredential(), "scope-a")
    async with AsyncPipeline(client, policies=[policy]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 15 + "2d")))

    assert client.bodies[0].closed, "discarded 401 response leaked: body was not closed"
    assert not client.bodies[1].closed, "final response handed to caller must stay open"


# --------------------------------------------------------------------------- #
# Replay is gated on body replayability — identically on both pillars          #
# --------------------------------------------------------------------------- #


class _AlwaysRetry(BearerTokenPolicy):
    """Bearer policy whose hook always authorizes a single replay."""

    def on_challenge(self, request: Request, response: Response) -> bool:
        del request, response
        return True


class _AsyncAlwaysRetry(AsyncBearerTokenPolicy):
    """Async twin of ``_AlwaysRetry``."""

    async def on_challenge(self, request: Request, response: AsyncResponse) -> bool:
        del request, response
        return True


def test_bearer_replays_replayable_body_after_challenge() -> None:
    """A replayable body is re-issued once when the hook authorizes it."""
    client = _ScriptedClient([(Status.UNAUTHORIZED, 'Bearer realm="x"'), (Status.OK, None)])
    policy = _AlwaysRetry(_StaticCredential(), "scope-a")
    with Pipeline(client, policies=[policy]) as p:
        response = p.run(_request(), DispatchContext(_instr("0" * 15 + "40")))
    assert response.status is Status.OK
    assert len(client.calls) == 2


@pytest.mark.req("AUTH-31", "BODY-4")
def test_bearer_surfaces_401_for_nonreplayable_body() -> None:
    """A single-use body surfaces the 401 un-replayed instead of resending it."""
    client = _ScriptedClient([(Status.UNAUTHORIZED, 'Bearer realm="x"')])
    body = RequestBody.from_iter(iter([b"hello", b"world"]))
    request = Request(method=Method.POST, url=Url.parse("https://api.example.com/"), body=body)
    policy = _AlwaysRetry(_StaticCredential(), "scope-a")
    with Pipeline(client, policies=[policy]) as p:
        response = p.run(request, DispatchContext(_instr("0" * 15 + "41")))
    assert response.status is Status.UNAUTHORIZED
    # No replay: the single-use body was never re-sent, so it stays intact.
    assert len(client.calls) == 1
    assert not body.is_replayable()


async def test_async_bearer_replays_replayable_body_after_challenge() -> None:
    """Async twin: a replayable body is re-issued once on challenge."""
    client = _ScriptedAsyncClient(
        [(Status.UNAUTHORIZED, "WWW-Authenticate", 'Bearer realm="x"'), (Status.OK, None, None)]
    )
    policy = _AsyncAlwaysRetry(_StaticAsyncCredential(), "scope-a")
    async with AsyncPipeline(client, policies=[policy]) as p:
        response = await p.run(_request(), DispatchContext(_instr("0" * 15 + "42")))
    assert response.status is Status.OK
    assert len(client.calls) == 2


@pytest.mark.req("AUTH-31")
async def test_async_bearer_surfaces_401_for_nonreplayable_body() -> None:
    """Async twin: a single-use body surfaces the 401 un-replayed."""
    client = _ScriptedAsyncClient([(Status.UNAUTHORIZED, "WWW-Authenticate", 'Bearer realm="x"')])
    body = RequestBody.from_iter(iter([b"hello", b"world"]))
    request = Request(method=Method.POST, url=Url.parse("https://api.example.com/"), body=body)
    policy = _AsyncAlwaysRetry(_StaticAsyncCredential(), "scope-a")
    async with AsyncPipeline(client, policies=[policy]) as p:
        response = await p.run(request, DispatchContext(_instr("0" * 15 + "43")))
    assert response.status is Status.UNAUTHORIZED
    assert len(client.calls) == 1
    assert not body.is_replayable()


@pytest.mark.req("AUTH-33")
async def test_async_bearer_surfaces_401_without_challenge() -> None:
    """Async twin: a 401 without ``WWW-Authenticate`` is surfaced unchanged."""
    client = _CapturingAsyncClient(status=Status.UNAUTHORIZED)
    cred = _StaticAsyncCredential()
    policy = AsyncBearerTokenPolicy(cred, "scope-a")
    async with AsyncPipeline(client, policies=[policy]) as p:
        response = await p.run(_request(), DispatchContext(_instr("0" * 15 + "44")))
    assert response.status is Status.UNAUTHORIZED
    assert len(client.calls) == 1
    assert cred.calls == 1


# --------------------------------------------------------------------------- #
# A throwing challenge hook closes the 401 before the exception propagates      #
# --------------------------------------------------------------------------- #


@pytest.mark.req("AUTH-32")
def test_sync_on_challenge_throw_closes_401_before_propagating() -> None:
    """A raising sync hook releases the open 401 before re-raising."""
    client = _BodyScriptedClient([(Status.UNAUTHORIZED, 'Bearer realm="x"')])

    class _Boom(BearerTokenPolicy):
        def on_challenge(self, request: Request, response: Response) -> bool:
            del request, response
            raise RuntimeError("boom")

    policy = _Boom(_StaticCredential(), "scope-a")
    with (
        Pipeline(client, policies=[policy]) as p,
        pytest.raises(RuntimeError, match="boom"),
    ):
        p.run(_request(), DispatchContext(_instr("0" * 15 + "50")))
    assert client.bodies[0].closed, "throwing on_challenge leaked the 401 body"


@pytest.mark.req("AUTH-32")
async def test_async_on_challenge_coroutine_throw_closes_401() -> None:
    """A raising async coroutine hook releases the open 401 before re-raising."""
    client = _AsyncBodyScriptedClient([(Status.UNAUTHORIZED, 'Bearer realm="x"')])

    class _Boom(AsyncBearerTokenPolicy):
        async def on_challenge(self, request: Request, response: AsyncResponse) -> bool:
            del request, response
            raise RuntimeError("boom")

    policy = _Boom(_StaticAsyncCredential(), "scope-a")
    async with AsyncPipeline(client, policies=[policy]) as p:
        with pytest.raises(RuntimeError, match="boom"):
            await p.run(_request(), DispatchContext(_instr("0" * 15 + "51")))
    assert client.bodies[0].closed, "throwing async on_challenge leaked the 401 body"


@pytest.mark.req("AUTH-32", "AUTH-38")
async def test_async_on_challenge_synchronous_throw_closes_401() -> None:
    """A hook that throws synchronously (before a coroutine exists) still closes."""
    client = _AsyncBodyScriptedClient([(Status.UNAUTHORIZED, 'Bearer realm="x"')])

    class _Boom(AsyncBearerTokenPolicy):
        def on_challenge(  # type: ignore[override]
            self, request: Request, response: AsyncResponse
        ) -> bool:
            # Raises when *called*, before any awaitable is produced — the
            # third hook-failure shape the async gate must guard against.
            del request, response
            raise RuntimeError("boom")

    policy = _Boom(_StaticAsyncCredential(), "scope-a")
    async with AsyncPipeline(client, policies=[policy]) as p:
        with pytest.raises(RuntimeError, match="boom"):
            await p.run(_request(), DispatchContext(_instr("0" * 15 + "52")))
    assert client.bodies[0].closed, "synchronous on_challenge throw leaked the 401 body"


# --------------------------------------------------------------------------- #
# Eviction matches the stamped token, sparing a concurrently-refreshed one      #
# --------------------------------------------------------------------------- #


@pytest.mark.req("AUTH-36")
def test_bearer_eviction_matches_stamped_token_only() -> None:
    """A stale 401 must not evict a token a concurrent refresh already replaced."""
    cache = InMemoryTokenCache()
    fresh = AccessTokenInfo(token="fresh", expires_on=int(time.time()) + 3600)

    class _SwappingClient(HttpClient):
        def __init__(self) -> None:
            self.calls: list[Request] = []

        def execute(self, request: Request) -> Response:
            from dexpace.sdk.core.http.common import Headers

            self.calls.append(request)
            # A concurrent refresh lands before this stale 401 is handled: the
            # cache now holds a different, still-valid token.
            cache.set(("scope-a",), fresh)
            return Response(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=Status.UNAUTHORIZED,
                headers=Headers([]),
            )

    policy = BearerTokenPolicy(_StaticCredential(token="stale"), "scope-a", cache=cache)
    with Pipeline(_SwappingClient(), policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 15 + "45")))
    # The stale 401 carried "Bearer stale"; eviction keyed on the stamped
    # header must leave the newer "Bearer fresh" token untouched.
    surviving = cache.get(("scope-a",))
    assert surviving is not None
    assert surviving.token == "fresh"


@pytest.mark.req("AUTH-36")
async def test_async_bearer_eviction_matches_stamped_token_only() -> None:
    """Async twin: a stale 401 leaves a concurrently-refreshed token intact."""
    cache = InMemoryTokenCache()
    fresh = AccessTokenInfo(token="fresh", expires_on=int(time.time()) + 3600)

    class _SwappingAsyncClient(AsyncHttpClient):
        def __init__(self) -> None:
            self.calls: list[Request] = []

        async def execute(self, request: Request) -> AsyncResponse:
            from dexpace.sdk.core.http.common import Headers

            self.calls.append(request)
            cache.set(("scope-a",), fresh)
            return AsyncResponse(
                request=request,
                protocol=Protocol.HTTP_1_1,
                status=Status.UNAUTHORIZED,
                headers=Headers([]),
            )

    policy = AsyncBearerTokenPolicy(_StaticAsyncCredential(token="stale"), "scope-a", cache=cache)
    async with AsyncPipeline(_SwappingAsyncClient(), policies=[policy]) as p:
        await p.run(_request(), DispatchContext(_instr("0" * 15 + "46")))
    surviving = cache.get(("scope-a",))
    assert surviving is not None
    assert surviving.token == "fresh"


# --------------------------------------------------------------------------- #
# Credential hygiene: the HTTPS-only guard fires before any credential is       #
# fetched or stamped, and before the transport is ever dialed.                  #
# --------------------------------------------------------------------------- #


class _NeverDialClient(HttpClient):
    """Sync transport that fails loudly if it is ever asked to execute."""

    def __init__(self) -> None:
        self.dialed = False

    def execute(self, request: Request) -> Response:
        del request
        self.dialed = True
        raise AssertionError("transport dialed despite the HTTPS-only guard")


class _NeverDialAsyncClient(AsyncHttpClient):
    """Async twin of ``_NeverDialClient``."""

    def __init__(self) -> None:
        self.dialed = False

    async def execute(self, request: Request) -> AsyncResponse:
        del request
        self.dialed = True
        raise AssertionError("transport dialed despite the HTTPS-only guard")


class _RecordingTokenCredential:
    """TokenCredential that records whether ``get_token_info`` was called."""

    def __init__(self, token: str = "s3cr3t-token") -> None:
        self.calls = 0
        self._token = token

    def get_token_info(self, *scopes: str, options: object = None) -> AccessTokenInfo:
        del scopes, options
        self.calls += 1
        return AccessTokenInfo(token=self._token, expires_on=int(time.time()) + 3600)

    def close(self) -> None:
        return None


class _RecordingAsyncTokenCredential:
    """Async twin of ``_RecordingTokenCredential``."""

    def __init__(self, token: str = "s3cr3t-token") -> None:
        self.calls = 0
        self._token = token

    async def get_token_info(self, *scopes: str, options: object = None) -> AccessTokenInfo:
        del scopes, options
        self.calls += 1
        return AccessTokenInfo(token=self._token, expires_on=int(time.time()) + 3600)

    async def close(self) -> None:
        return None


class _RecordingKeyCredential(KeyCredential):
    """KeyCredential that counts reads of ``key`` to prove the guard runs first."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key_reads = 0

    @property
    def key(self) -> str:
        self.key_reads += 1
        return self._key


class _RecordingBasicCredential(BasicAuthCredential):
    """BasicAuthCredential that counts reads of ``encoded`` to prove the order."""

    def __init__(self, username: str, password: str) -> None:
        super().__init__(username, password)
        self.encoded_reads = 0

    @property
    def encoded(self) -> str:
        self.encoded_reads += 1
        return self._encoded


@pytest.mark.req("AUTH-28", "XCUT-16")
def test_bearer_policy_https_guard_precedes_fetch_and_dial() -> None:
    """An http:// URL errors before the credential is fetched or transport dialed."""
    client = _NeverDialClient()
    cred = _RecordingTokenCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p, pytest.raises(ServiceRequestError):
        p.run(
            _request("http://insecure.example.com/"),
            DispatchContext(_instr("0" * 15 + "40")),
        )
    assert cred.calls == 0, "credential was fetched before the HTTPS guard fired"
    assert client.dialed is False, "transport was dialed before the HTTPS guard fired"


@pytest.mark.req("AUTH-38")
async def test_async_bearer_policy_https_guard_precedes_fetch_and_dial() -> None:
    """Async twin: http:// errors before the async fetch or async dial."""
    client = _NeverDialAsyncClient()
    cred = _RecordingAsyncTokenCredential()
    policy = AsyncBearerTokenPolicy(cred, "scope-a")
    async with AsyncPipeline(client, policies=[policy]) as p:
        with pytest.raises(ServiceRequestError):
            await p.run(
                _request("http://insecure.example.com/"),
                DispatchContext(_instr("0" * 15 + "41")),
            )
    assert cred.calls == 0, "credential was fetched before the HTTPS guard fired"
    assert client.dialed is False, "transport was dialed before the HTTPS guard fired"


@pytest.mark.req("AUTH-28")
def test_key_credential_policy_https_guard_precedes_stamp_and_dial() -> None:
    """An http:// URL errors before the key is read or the transport dialed."""
    client = _NeverDialClient()
    cred = _RecordingKeyCredential("hunter2")
    policy = KeyCredentialPolicy(cred, "X-API-Key")
    with Pipeline(client, policies=[policy]) as p, pytest.raises(ServiceRequestError):
        p.run(
            _request("http://insecure.example.com/"),
            DispatchContext(_instr("0" * 15 + "42")),
        )
    assert cred.key_reads == 0, "key was read before the HTTPS guard fired"
    assert client.dialed is False, "transport was dialed before the HTTPS guard fired"


@pytest.mark.req("AUTH-28")
def test_basic_auth_policy_https_guard_precedes_stamp_and_dial() -> None:
    """An http:// URL errors before the payload is read or the transport dialed."""
    client = _NeverDialClient()
    cred = _RecordingBasicCredential("user", "pass")
    policy = BasicAuthPolicy(cred)
    with Pipeline(client, policies=[policy]) as p, pytest.raises(ServiceRequestError):
        p.run(
            _request("http://insecure.example.com/"),
            DispatchContext(_instr("0" * 15 + "43")),
        )
    assert cred.encoded_reads == 0, "payload was read before the HTTPS guard fired"
    assert client.dialed is False, "transport was dialed before the HTTPS guard fired"


def test_key_credential_policy_https_opt_out_stamps_over_http() -> None:
    """``enforce_https=False`` lets the key policy stamp over plain HTTP."""
    client = _CapturingClient()
    policy = KeyCredentialPolicy(KeyCredential("hunter2"), "X-API-Key")
    with Pipeline(client, policies=[policy]) as p:
        p.run(
            _request("http://insecure.example.com/"),
            DispatchContext(_instr("0" * 15 + "45")),
            enforce_https=False,
        )
    assert client.calls[0].headers.get("x-api-key") == "hunter2"


def test_basic_auth_policy_https_opt_out_stamps_over_http() -> None:
    """``enforce_https=False`` lets the basic policy stamp over plain HTTP."""
    client = _CapturingClient()
    policy = BasicAuthPolicy(BasicAuthCredential("user", "pass"))
    with Pipeline(client, policies=[policy]) as p:
        p.run(
            _request("http://insecure.example.com/"),
            DispatchContext(_instr("0" * 15 + "46")),
            enforce_https=False,
        )
    assert client.calls[0].headers.get("authorization") == "Basic dXNlcjpwYXNz"


# --------------------------------------------------------------------------- #
# Credential hygiene: every credentialed policy redacts secrets in repr/str.    #
# --------------------------------------------------------------------------- #


@pytest.mark.req("AUTH-8")
def test_credentialed_policy_reprs_redact_secrets() -> None:
    """Render every credentialed policy and grep for the literal secrets."""
    secret_key = "hunter2-key"
    secret_user = "alice-user"
    secret_pass = "hunter2-pass"
    secret_token = "s3cr3t-token"
    encoded = BasicAuthCredential(secret_user, secret_pass).encoded
    policies = [
        KeyCredentialPolicy(KeyCredential(secret_key), "X-API-Key", prefix="SharedKey"),
        BasicAuthPolicy(BasicAuthCredential(secret_user, secret_pass)),
        BearerTokenPolicy(_RecordingTokenCredential(secret_token), "scope-a"),
        AsyncBearerTokenPolicy(_RecordingAsyncTokenCredential(secret_token), "scope-a"),
    ]
    needles = [secret_key, secret_user, secret_pass, secret_token, encoded]
    for policy in policies:
        for rendered in (repr(policy), str(policy)):
            for needle in needles:
                assert needle not in rendered, (
                    f"{type(policy).__name__} leaked a secret in {rendered!r}"
                )


# --------------------------------------------------------------------------- #
# Credential hygiene: blank secrets are rejected at construction, not first use. #
# --------------------------------------------------------------------------- #


def test_key_credential_policy_rejects_blank_secret_at_construction() -> None:
    with pytest.raises((TypeError, ValueError)):
        KeyCredentialPolicy(KeyCredential(""), "X-API-Key")


def test_basic_auth_policy_rejects_blank_secret_at_construction() -> None:
    with pytest.raises(ValueError):
        BasicAuthPolicy(BasicAuthCredential("user", ""))
    with pytest.raises(ValueError):
        BasicAuthPolicy(BasicAuthCredential("", "pass"))


# --------------------------------------------------------------------------- #
# Credential hygiene: KeyCredentialPolicy header/prefix shape.                  #
# --------------------------------------------------------------------------- #


def test_key_credential_policy_rejects_empty_header_name() -> None:
    with pytest.raises(ValueError):
        KeyCredentialPolicy(KeyCredential("k"), "")


def test_key_credential_policy_rejects_non_key_credential() -> None:
    with pytest.raises(TypeError):
        KeyCredentialPolicy(object(), "X-API-Key")  # type: ignore[arg-type]


@pytest.mark.req("AUTH-26")
def test_key_credential_policy_no_prefix_by_default() -> None:
    client = _CapturingClient()
    policy = KeyCredentialPolicy(KeyCredential("k"), "X-API-Key")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 15 + "47")))
    assert client.calls[0].headers.get("x-api-key") == "k"


# --------------------------------------------------------------------------- #
# Credential hygiene: key-style policies/credentials are identity-equal, while  #
# the bearer-style access-token value object keeps value equality.              #
# --------------------------------------------------------------------------- #


def test_key_style_policies_use_identity_equality() -> None:
    """Two policies with the same key value must not compare equal."""
    a = KeyCredentialPolicy(KeyCredential("same"), "X-API-Key")
    b = KeyCredentialPolicy(KeyCredential("same"), "X-API-Key")
    assert a == a
    assert a != b


@pytest.mark.req("AUTH-8")
def test_key_style_credentials_use_identity_equality() -> None:
    assert KeyCredential("same") != KeyCredential("same")
    assert BasicAuthCredential("u", "p") != BasicAuthCredential("u", "p")


@pytest.mark.req("AUTH-8")
def test_bearer_style_access_token_uses_value_equality() -> None:
    a = AccessTokenInfo(token="t", expires_on=100, token_type="Bearer")
    b = AccessTokenInfo(token="t", expires_on=100, token_type="Bearer")
    assert a == b
