# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Real-adapter conformance harness for the httpx transports.

Implements the shared transport-adapter conformance battery's
``SyncTransportHarness`` / ``AsyncTransportHarness`` ABCs (from the installed
``dexpace.sdk.tck.harness`` module) against the REAL `HttpxHttpClient` /
`AsyncHttpxHttpClient` adapters — not a stand-in.

The "native layer" faked here is a custom `httpx.BaseTransport` /
`httpx.AsyncBaseTransport` pair, plugged in through the same ``transport=``
extension point the adapters already document as their test seam. Everything
above that boundary — request building, content-type resolution, framing,
per-call timeout, exception mapping, response adaptation, streaming, close —
runs as real `httpx` / adapter code; only the socket is faked out, exactly
where `httpx.MockTransport` would sit.

This module intentionally lives in ``dexpace-sdk-http-httpx``'s own test tree
(not inside the TCK): the harness ABCs ship in the installed
``dexpace.sdk.tck`` package, so registering from here keeps this package's
certification self-contained — no shared file is edited, so parallel
certification work for the other transports never collides with this one.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Final

import httpx

from dexpace.sdk.core.errors import RequestCancelledError
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.http.httpx import AsyncHttpxHttpClient, HttpxHttpClient
from dexpace.sdk.tck.harness import (
    AsyncTransportHarness,
    ClosableNative,
    DropLog,
    Outcome,
    OutcomeKind,
    ResponseSpec,
    SentRequest,
    SyncTransportHarness,
)

if TYPE_CHECKING:
    from dexpace.sdk.core.client.async_http_client import AsyncHttpClient
    from dexpace.sdk.core.client.http_client import HttpClient

_DEFAULT_TIMEOUT: Final[float] = 30.0


def _resolve_timeouts(options: dict[str, object]) -> tuple[float, float, float, float]:
    """Resolve the harness's ``timeout`` option into four equal phase timeouts."""
    timeout = options.get("timeout")
    if timeout is None:
        return (_DEFAULT_TIMEOUT, _DEFAULT_TIMEOUT, _DEFAULT_TIMEOUT, _DEFAULT_TIMEOUT)
    assert isinstance(timeout, int | float)
    value = float(timeout)
    return (value, value, value, value)


def _to_sdk_headers(httpx_headers: httpx.Headers) -> Headers:
    """Convert httpx's own header set into the SDK's `Headers` value type."""
    return Headers(httpx_headers.multi_items())


def _extract_timeout(request: httpx.Request) -> float | None:
    """Return the effective per-call timeout httpx resolved for ``request``.

    By the time a transport's ``handle_request`` runs, ``httpx.Client.send``
    has already populated ``request.extensions["timeout"]`` — either from a
    per-call override (``HttpxHttpClient`` passes ``request.timeout`` to
    ``build_request``) or the client's own configured default. All four
    phases are set equal by this adapter, so reading any one is
    representative.
    """
    extensions = request.extensions.get("timeout")
    if not extensions:
        return None
    value = extensions.get("read")
    return None if value is None else float(value)


def _response_header_pairs(spec: ResponseSpec) -> list[tuple[bytes, bytes]]:
    """Encode response headers as wire-form (Latin-1) byte pairs.

    Real HTTP libraries decode header bytes as Latin-1 (obs-text tolerant) —
    that is what lets a raw byte like ``0xE9`` survive as ``café``'s ``é``.
    Passing plain ``str`` pairs to ``httpx.Headers`` instead would reject
    anything outside ASCII, so headers are encoded here exactly as a real
    transport would hand them to httpx off the wire.
    """
    return [(name.encode("latin-1"), value.encode("latin-1")) for name, value in spec.headers]


def _raise_for_outcome(outcome: Outcome, request: httpx.Request) -> None:
    """Raise the native failure a programmed non-``RESPONSE`` outcome models."""
    kind = outcome.kind
    if kind is OutcomeKind.CONNECT_ERROR:
        raise httpx.ConnectError("connect failed", request=request)
    if kind is OutcomeKind.CONNECT_TIMEOUT:
        raise httpx.ConnectTimeout("connect timed out", request=request)
    if kind is OutcomeKind.READ_TIMEOUT:
        raise httpx.ReadTimeout("read timed out", request=request)
    if kind is OutcomeKind.CANCELLED:
        # A synchronous httpx client has no native cancellation primitive of
        # its own — cooperative cancellation is a caller-side concept. A
        # custom transport (this adapter's documented BYO extension point)
        # can already raise an SDK error directly and have it pass through
        # unmapped (it matches none of ``execute``'s ``except httpx.*``
        # clauses), which is exactly what is simulated here.
        raise RequestCancelledError("request cancelled")
    if kind is OutcomeKind.PROTOCOL_ERROR:
        raise httpx.RemoteProtocolError("protocol error", request=request)


def _build_httpx_response(
    spec: ResponseSpec,
    stream: httpx.SyncByteStream | httpx.AsyncByteStream,
    request: httpx.Request,
) -> httpx.Response:
    """Build the native httpx.Response the adapter will adapt."""
    extensions: dict[str, bytes] = {"http_version": spec.protocol.encode("ascii")}
    if spec.reason is not None:
        extensions["reason_phrase"] = spec.reason.encode("ascii")
    return httpx.Response(
        status_code=spec.status,  # type: ignore[arg-type]  # deliberately non-int for adaptation-failure tests
        headers=_response_header_pairs(spec),
        stream=stream,
        request=request,
        extensions=extensions,
    )


class _SyncFakeStream(httpx.SyncByteStream):
    """Programmable native byte stream whose close the battery can observe."""

    __slots__ = ("_spec", "closed")

    def __init__(self, spec: ResponseSpec) -> None:
        self._spec = spec
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        if self._spec.stream:
            yield from self._spec.chunks
        else:
            yield self._spec.body

    def close(self) -> None:
        """Release the native stream. Idempotent."""
        self.closed = True


class _AsyncFakeStream(httpx.AsyncByteStream):
    """Async twin of `_SyncFakeStream`, with a blockable producer."""

    __slots__ = ("_released", "_spec", "closed")

    def __init__(self, spec: ResponseSpec) -> None:
        self._spec = spec
        self.closed = False
        self._released = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._spec.stream:
            for chunk in self._spec.chunks:
                yield chunk
        else:
            yield self._spec.body
        if self._spec.stream and self._spec.block_after_chunks:
            # Model a producer that would block waiting for more data; only a
            # close (early abandon / cancellation) unblocks it.
            await self._released.wait()

    async def aclose(self) -> None:
        """Release the native stream and unblock the producer. Idempotent."""
        self.closed = True
        self._released.set()


class _HttpxFakeCore:
    """Programmable state shared by a harness and its fake transport."""

    def __init__(self) -> None:
        self._queue: deque[Outcome] = deque()
        self.sent: list[SentRequest] = []
        self.attempts = 0
        self.responses: list[ClosableNative] = []
        self._lock = threading.Lock()

    def program(self, outcome: Outcome) -> None:
        """Queue one outcome for the next exchange."""
        with self._lock:
            self._queue.append(outcome)

    def take(self) -> Outcome:
        """Consume the next programmed outcome, counting the attempt."""
        with self._lock:
            self.attempts += 1
            if not self._queue:
                raise AssertionError("no transport outcome was programmed")
            return self._queue.popleft()

    def record(self, request: httpx.Request) -> None:
        """Record a request that reached the fake native transport."""
        sent = SentRequest(
            method=request.method,
            url=str(request.url),
            headers=_to_sdk_headers(request.headers),
            body=request.content,
            timeout=_extract_timeout(request),
        )
        with self._lock:
            self.sent.append(sent)


class _HttpxFakeTransport(httpx.BaseTransport):
    """Fake synchronous httpx transport driving the shared conformance battery.

    Sits exactly where a real ``httpx.HTTPTransport`` would: below
    ``httpx.Client``'s request-building / redirect logic, above the socket.
    """

    def __init__(self, core: _HttpxFakeCore) -> None:
        self._core = core

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Resolve the next programmed outcome for ``request``."""
        request.read()
        self._core.record(request)
        outcome = self._core.take()
        if outcome.kind is not OutcomeKind.RESPONSE:
            _raise_for_outcome(outcome, request)
        assert outcome.response is not None
        stream = _SyncFakeStream(outcome.response)
        self._core.responses.append(stream)
        return _build_httpx_response(outcome.response, stream, request)


class _HttpxFakeAsyncTransport(httpx.AsyncBaseTransport):
    """Async twin of `_HttpxFakeTransport`."""

    def __init__(self, core: _HttpxFakeCore) -> None:
        self._core = core

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Resolve the next programmed outcome for ``request``."""
        await request.aread()
        self._core.record(request)
        outcome = self._core.take()
        if outcome.kind is not OutcomeKind.RESPONSE:
            _raise_for_outcome(outcome, request)
        assert outcome.response is not None
        stream = _AsyncFakeStream(outcome.response)
        self._core.responses.append(stream)
        return _build_httpx_response(outcome.response, stream, request)


class _ByoClientView:
    """Exposes ``.closed`` for a BYO httpx client, matching `ClosableNative`."""

    __slots__ = ("_client",)

    def __init__(self, client: httpx.Client | httpx.AsyncClient) -> None:
        self._client = client

    @property
    def closed(self) -> bool:
        """Whether the underlying httpx client has been closed."""
        return self._client.is_closed


class _HttpxHarnessMixin:
    """Shared programming / introspection for the httpx harnesses.

    Neither facet below exists in the real httpx adapters, so both
    capabilities are turned off rather than faked: the adapters have no
    pre-emptive outbound header-rejection mechanism (unlike the in-memory
    reference fake), and no proxy-configuration surface at all yet.
    """

    supports_native_header_rejection = False
    supports_proxy_introspection = False
    min_timeout_granularity = 1e-6

    def __init__(self) -> None:
        self._core = _HttpxFakeCore()
        self._byo: _ByoClientView | None = None

    def expect(self, outcome: Outcome) -> None:
        """Program the next native exchange to resolve as ``outcome``."""
        self._core.program(outcome)

    def sent_requests(self) -> list[SentRequest]:
        """Return, in order, every request the adapter handed to httpx."""
        return self._core.sent

    def attempt_count(self) -> int:
        """Return how many native exchanges the adapter initiated."""
        return self._core.attempts

    def drop_log(self) -> DropLog:
        """Return an (always-empty) drop log; this facet is unsupported."""
        return DropLog()

    def native_responses(self) -> list[ClosableNative]:
        """Return the native streams produced so far (for close checks)."""
        return self._core.responses

    def mark_native_rejected(self, *names: str) -> None:
        """No-op: the httpx adapter has no native-rejection facet to model."""
        del names

    def byo_underlying(self) -> ClosableNative | None:
        """Return the caller-supplied native client, if one was built."""
        return self._byo


class HttpxSyncHarness(_HttpxHarnessMixin, SyncTransportHarness):
    """Synchronous harness backed by the real `HttpxHttpClient` adapter."""

    def __init__(self) -> None:
        super().__init__()
        self._transport = _HttpxFakeTransport(self._core)

    def new_client(self, **options: object) -> HttpClient:
        """Build an `HttpxHttpClient` over the fake transport."""
        connect, read, write, pool = _resolve_timeouts(options)
        if bool(options.get("byo", False)):
            httpx_client = httpx.Client(
                transport=self._transport, follow_redirects=False, trust_env=False
            )
            self._byo = _ByoClientView(httpx_client)
            return HttpxHttpClient(client=httpx_client)
        return HttpxHttpClient(
            connect_timeout=connect,
            read_timeout=read,
            write_timeout=write,
            pool_timeout=pool,
            transport=self._transport,
        )

    def close_client(self, client: HttpClient) -> None:
        """Close a sync adapter via its ``close`` surface."""
        assert isinstance(client, HttpxHttpClient)
        client.close()


class HttpxAsyncHarness(_HttpxHarnessMixin, AsyncTransportHarness):
    """Asynchronous harness backed by the real `AsyncHttpxHttpClient` adapter."""

    def __init__(self) -> None:
        super().__init__()
        self._transport = _HttpxFakeAsyncTransport(self._core)

    def new_async_client(self, **options: object) -> AsyncHttpClient:
        """Build an `AsyncHttpxHttpClient` over the fake transport."""
        connect, read, write, pool = _resolve_timeouts(options)
        if bool(options.get("byo", False)):
            httpx_client = httpx.AsyncClient(
                transport=self._transport, follow_redirects=False, trust_env=False
            )
            self._byo = _ByoClientView(httpx_client)
            return AsyncHttpxHttpClient(client=httpx_client)
        return AsyncHttpxHttpClient(
            connect_timeout=connect,
            read_timeout=read,
            write_timeout=write,
            pool_timeout=pool,
            transport=self._transport,
        )

    async def aclose_client(self, client: AsyncHttpClient) -> None:
        """Close an async adapter via its ``aclose`` surface."""
        assert isinstance(client, AsyncHttpxHttpClient)
        await client.aclose()


__all__ = ["HttpxAsyncHarness", "HttpxSyncHarness"]
