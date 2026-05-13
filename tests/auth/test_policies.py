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
    BasicAuthCredential,
    BasicAuthPolicy,
    BearerTokenPolicy,
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

    def __init__(self, token: str = "abc", expires_in: int = 3600) -> None:
        self.calls = 0
        self.token = token
        self.expires_in = expires_in

    def get_token_info(
        self,
        *scopes: str,
        options: object = None,
    ) -> AccessTokenInfo:
        del scopes, options
        self.calls += 1
        return AccessTokenInfo(
            token=self.token,
            expires_on=int(time.time()) + self.expires_in,
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

    def __init__(self, delay: float = 0.05) -> None:
        self.calls = 0
        self._delay = delay
        self._lock = threading.Lock()

    def get_token_info(
        self,
        *scopes: str,
        options: object = None,
    ) -> AccessTokenInfo:
        del scopes, options
        with self._lock:
            self.calls += 1
        time.sleep(self._delay)
        return AccessTokenInfo(token="abc", expires_on=int(time.time()) + 3600)

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

    def __init__(self, delay: float = 0.05) -> None:
        self.calls = 0
        self._delay = delay

    async def get_token_info(
        self,
        *scopes: str,
        options: object = None,
    ) -> AccessTokenInfo:
        del scopes, options
        self.calls += 1
        await asyncio.sleep(self._delay)
        return AccessTokenInfo(token="abc", expires_on=int(time.time()) + 3600)

    async def close(self) -> None:
        return None


class _CapturingAsyncClient(AsyncHttpClient):
    """Async twin of ``_CapturingClient``."""

    def __init__(self, *, status: Status = Status.OK) -> None:
        self.status = status
        self.calls: list[Request] = []

    async def execute(self, request: Request) -> AsyncResponse:
        self.calls.append(request)
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=self.status,
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
