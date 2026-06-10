# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the built-in authentication policies."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
from dexpace.sdk.core.client.http_client import HttpClient
from dexpace.sdk.core.errors import ClientAuthenticationError, ServiceRequestError
from dexpace.sdk.core.http.auth import (
    AccessTokenInfo,
    AsyncBearerTokenPolicy,
    AuthenticateChallenge,
    BasicAuthCredential,
    BasicAuthPolicy,
    BasicChallengeHandler,
    BearerTokenPolicy,
    DigestChallengeHandler,
    KeyCredential,
    KeyCredentialPolicy,
)
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


def test_key_credential_policy_stamps_header() -> None:
    client = _CapturingClient()
    policy = KeyCredentialPolicy(KeyCredential("hunter2"), "X-API-Key")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "1")))
    assert client.calls[0].headers.get("x-api-key") == "hunter2"


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


def test_bearer_token_policy_stamps_header() -> None:
    client = _CapturingClient()
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p:
        p.run(_request(), DispatchContext(_instr("0" * 16 + "4")))
    assert client.calls[0].headers.get("authorization") == "Bearer abc"


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


def test_bearer_token_policy_enforces_https() -> None:
    client = _CapturingClient()
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p, pytest.raises(ServiceRequestError):
        p.run(
            _request("http://insecure.example.com/"),
            DispatchContext(_instr("0" * 16 + "7")),
        )


def test_bearer_token_policy_raises_on_401_without_challenge() -> None:
    client = _CapturingClient(status=Status.UNAUTHORIZED)
    cred = _StaticCredential()
    policy = BearerTokenPolicy(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p, pytest.raises(ClientAuthenticationError):
        p.run(_request(), DispatchContext(_instr("0" * 16 + "8")))


def test_bearer_token_policy_on_challenge_hook() -> None:
    """Subclass that handles the challenge by re-requesting once."""

    client = _CapturingClient(status=Status.UNAUTHORIZED, www_auth=True)
    cred = _StaticCredential()

    class _Retrying(BearerTokenPolicy):
        def on_challenge(self, request: Request, response: Response) -> bool:
            return True

    policy = _Retrying(cred, "scope-a")
    with Pipeline(client, policies=[policy]) as p, pytest.raises(ClientAuthenticationError):
        # Server keeps responding 401; eventually the policy gives up.
        p.run(_request(), DispatchContext(_instr("0" * 16 + "9")))
    # Two attempts: initial + one re-issue after challenge.
    assert len(client.calls) == 2


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


def test_bearer_token_policy_serializes_concurrent_refresh() -> None:
    """Concurrent sync sends must issue exactly one token fetch."""

    client = _CapturingClient()
    cred = _SlowCredential()
    policy = BearerTokenPolicy(cred, "scope-a")

    trace_ids = [f"{i:032x}" for i in range(1, 9)]

    def _send(trace: str) -> None:
        with Pipeline(client, policies=[policy]) as p:
            p.run(_request(), DispatchContext(_instr(trace)))

    threads = [threading.Thread(target=_send, args=(t,)) for t in trace_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

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
    with Pipeline(client, policies=[policy]) as p, pytest.raises(ClientAuthenticationError):
        p.run(_request(), DispatchContext(_instr("0" * 16 + "d")))

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
        with pytest.raises(ClientAuthenticationError):
            await p.run(_request(), DispatchContext(_instr("0" * 15 + "14")))

    assert handler.can_handle_calls == 1
    assert handler.handle_calls == 0
    assert on_challenge_calls == 1
