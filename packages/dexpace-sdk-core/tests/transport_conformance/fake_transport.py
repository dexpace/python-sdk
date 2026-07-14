# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""In-memory reference transport that satisfies the conformance battery.

This is a *reference adapter* over a *fake native layer*, not a shortcut: it
implements the same behaviours a real adapter (httpx / requests / aiohttp /
stdlib) must — content-type resolution, adapter-computed framing, native-rejected
header dropping, defensive inbound-header handling, native-error mapping,
ownership-aware close, single-use body consumption, lazy streaming with a
close cascade, and per-call timeout clamping — so proving the battery green
against it proves the battery is a genuine contract, not a tautology.

The fake native layer (`_NativeCore`, `_SyncNativeResponse`,
`_AsyncNativeResponse`) never follows a 3xx and never retries on its own: it
does exactly what it is programmed to do, once. That is the whole point of the
redirect / retry contract — the SDK's own policies own those behaviours, the
adapter is inert.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from types import TracebackType
from typing import TYPE_CHECKING, Final, Self

from dexpace.sdk.core.errors import (
    RequestCancelledError,
    SdkError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.media_type import MediaType
from dexpace.sdk.core.http.common.protocol import Protocol
from dexpace.sdk.core.http.response.async_response import AsyncResponse
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.response import Response
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.http.response.status import Status

from .harness import (
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
    from dexpace.sdk.core.http.request.request import Request

_LOGGER: Final = logging.getLogger("dexpace.sdk.transport_conformance.fake")

_DEFAULT_TIMEOUT: Final[float] = 30.0
#: Methods whose semantics call for an explicit zero-length body substitution
#: when the caller supplies no payload (RFC 9110 §8.6).
_ZERO_LENGTH_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH"})
#: Proxy schemes this reference adapter can actually route through; anything
#: else is discoverable as unsupported rather than silently ignored.
_SUPPORTED_PROXY_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


# --------------------------------------------------------------------------- #
# Fake native error hierarchy.
#
# The two timeout types deliberately subclass ``TimeoutError`` (an ``OSError``)
# to reproduce the real ``socket.timeout`` ambiguity: a naive handler that
# caught ``OSError`` (or a cancellation type, also an ``OSError``) before the
# timeout type would mislabel a timeout. ``_map_native_error`` therefore checks
# the timeout types FIRST, by type, never by message text.
# --------------------------------------------------------------------------- #


class _FakeNativeError(Exception):
    """Root of the fake native-library error hierarchy."""


class _NativeConnectError(_FakeNativeError):
    """No response was ever produced (connection refused / DNS / reset)."""


class _NativeConnectTimeoutError(_FakeNativeError, TimeoutError):
    """The connect phase timed out before any byte was sent."""


class _NativeReadTimeoutError(_FakeNativeError, TimeoutError):
    """The read phase timed out after the request was transmitted."""


class _NativeCancelledError(_FakeNativeError):
    """The in-flight exchange was cooperatively cancelled."""


class _NativeProtocolError(_FakeNativeError):
    """A generic transport error occurred mid-exchange."""


_NATIVE_ERROR_BY_KIND: Final[dict[OutcomeKind, type[_FakeNativeError]]] = {
    OutcomeKind.CONNECT_ERROR: _NativeConnectError,
    OutcomeKind.CONNECT_TIMEOUT: _NativeConnectTimeoutError,
    OutcomeKind.READ_TIMEOUT: _NativeReadTimeoutError,
    OutcomeKind.CANCELLED: _NativeCancelledError,
    OutcomeKind.PROTOCOL_ERROR: _NativeProtocolError,
}


def _map_native_error(err: _FakeNativeError) -> SdkError:
    """Map a native error onto the SDK taxonomy, timeout-before-cancellation.

    Discrimination is by exception *type*, never message text, and the timeout
    types are matched before the cancellation type — both timeouts and
    ``RequestCancelledError`` are ``OSError`` subtypes, so an ordering that fell
    through to a broad ``OSError`` clause first would misclassify a timeout.

    Args:
        err: The fake native error raised for the exchange.

    Returns:
        The corresponding SDK error to raise.
    """
    if isinstance(err, _NativeConnectTimeoutError):
        return ServiceRequestTimeoutError("connect timed out", error=err)
    if isinstance(err, _NativeReadTimeoutError):
        return ServiceResponseTimeoutError("read timed out", error=err)
    if isinstance(err, _NativeCancelledError):
        return RequestCancelledError("request cancelled", error=err)
    if isinstance(err, _NativeConnectError):
        return ServiceRequestError("connect failed", error=err)
    return ServiceRequestError("transport error", error=err)


class _AdaptationError(Exception):
    """Raised internally when a native response cannot be adapted."""


# --------------------------------------------------------------------------- #
# Fake native responses (sync + async), each tracking its own close.
# --------------------------------------------------------------------------- #


class _SyncNativeResponse:
    """A programmable native response whose close the battery can observe."""

    __slots__ = ("_chunks", "closed", "spec")

    def __init__(self, spec: ResponseSpec) -> None:
        self.spec = spec
        self.closed = False
        source = spec.chunks if spec.stream else (spec.body,)
        self._chunks: deque[bytes] = deque(source)

    def next_chunk(self) -> bytes | None:
        """Pop the next body chunk, or ``None`` once exhausted/closed."""
        if self.closed or not self._chunks:
            return None
        return self._chunks.popleft()

    def close(self) -> None:
        """Release the native response. Idempotent."""
        self.closed = True


class _AsyncNativeResponse:
    """Async twin of `_SyncNativeResponse`, with a blockable producer."""

    __slots__ = ("_chunks", "_released", "closed", "spec")

    def __init__(self, spec: ResponseSpec) -> None:
        self.spec = spec
        self.closed = False
        source = spec.chunks if spec.stream else (spec.body,)
        self._chunks: deque[bytes] = deque(source)
        self._released = asyncio.Event()

    async def next_chunk(self) -> bytes | None:
        """Pop the next chunk, blocking after exhaustion when programmed to."""
        if self.closed:
            return None
        if self._chunks:
            return self._chunks.popleft()
        if self.spec.block_after_chunks:
            # Model a producer that would block waiting for more data; only a
            # close (early abandon) or a task cancellation unblocks it.
            await self._released.wait()
        return None

    async def close(self) -> None:
        """Release the native response and unblock the producer. Idempotent."""
        self.closed = True
        self._released.set()


class _SyncNativeReader:
    """``BinaryIO``-shaped, buffered reader over a `_SyncNativeResponse`."""

    __slots__ = ("_buf", "_done", "_native")

    def __init__(self, native: _SyncNativeResponse) -> None:
        self._native = native
        self._buf = bytearray()
        self._done = False

    def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes (all when ``size`` < 0); ``b""`` at EOF."""
        target = None if size < 0 else size
        while (target is None or len(self._buf) < target) and not self._done:
            chunk = self._native.next_chunk()
            if chunk is None:
                self._done = True
                break
            self._buf.extend(chunk)
        if target is None:
            out = bytes(self._buf)
            self._buf.clear()
            return out
        take = min(target, len(self._buf))
        out = bytes(self._buf[:take])
        del self._buf[:take]
        return out

    def close(self) -> None:
        """Cascade the close through to the native response."""
        self._native.close()


class _AsyncNativeReader:
    """``SupportsAsyncRead`` buffered reader over an `_AsyncNativeResponse`."""

    __slots__ = ("_buf", "_done", "_native")

    def __init__(self, native: _AsyncNativeResponse) -> None:
        self._native = native
        self._buf = bytearray()
        self._done = False

    async def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes (all when ``size`` < 0); ``b""`` at EOF."""
        target = None if size < 0 else size
        while (target is None or len(self._buf) < target) and not self._done:
            chunk = await self._native.next_chunk()
            if chunk is None:
                self._done = True
                break
            self._buf.extend(chunk)
        if target is None:
            out = bytes(self._buf)
            self._buf.clear()
            return out
        take = min(target, len(self._buf))
        out = bytes(self._buf[:take])
        del self._buf[:take]
        return out

    async def close(self) -> None:
        """Cascade the close through to the native response."""
        await self._native.close()


# --------------------------------------------------------------------------- #
# Proxy config + owned/BYO native client markers.
# --------------------------------------------------------------------------- #


class _ByoNativeClient:
    """Stand-in for a caller-supplied underlying client; tracks open/closed."""

    __slots__ = ("_open",)

    def __init__(self) -> None:
        self._open = True

    def close(self) -> None:
        """Close the underlying client. Idempotent."""
        self._open = False

    @property
    def is_open(self) -> bool:
        """Whether the underlying client is still usable."""
        return self._open

    @property
    def closed(self) -> bool:
        """Whether the underlying client has been closed."""
        return not self._open


class _ProxyConfig:
    """Proxy settings, exposing capability introspection and credential safety."""

    __slots__ = ("_password", "_scheme", "_user", "host", "requested_features")

    def __init__(
        self,
        url: str,
        requested_features: frozenset[str] = frozenset(),
    ) -> None:
        scheme, _, rest = url.partition("://")
        userinfo, _, host = rest.rpartition("@")
        user, _, password = userinfo.partition(":")
        self._scheme = scheme
        self._user = user
        self._password = password
        self.host = host
        self.requested_features = requested_features

    @property
    def redacted_url(self) -> str:
        """The proxy URL with any userinfo replaced by a redaction marker."""
        if self._user:
            return f"{self._scheme}://[REDACTED]@{self.host}"
        return f"{self._scheme}://{self.host}"

    def unsupported_features(self) -> frozenset[str]:
        """Return requested proxy features this adapter cannot honour."""
        unsupported = set(self.requested_features)
        if self._scheme not in _SUPPORTED_PROXY_SCHEMES:
            unsupported.add(f"scheme:{self._scheme}")
        return frozenset(unsupported)


# --------------------------------------------------------------------------- #
# The programmable native core, shared by an adapter and its harness.
# --------------------------------------------------------------------------- #


class _NativeCore:
    """The programmable native layer shared by an adapter and its harness."""

    def __init__(self) -> None:
        self._queue: deque[Outcome] = deque()
        self.sent: list[SentRequest] = []
        self.attempts = 0
        self.drop_log = DropLog()
        self.native_rejected: set[str] = set()
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

    def record_sent(self, sent: SentRequest) -> None:
        """Record a request that reached the native layer."""
        with self._lock:
            self.sent.append(sent)


def _clamp_timeout(timeout: float | None, granularity: float) -> float | None:
    """Clamp a positive sub-resolution timeout up to the minimum granularity.

    A tiny positive timeout (e.g. ``0.0001``) must never be truncated to zero,
    which would read as "no timeout"; it is raised to ``granularity`` instead.

    Args:
        timeout: The requested per-call timeout, or ``None`` for no timeout.
        granularity: The smallest positive timeout the adapter can apply.

    Returns:
        The effective timeout: ``None`` passes through; a positive value is at
        least ``granularity``.
    """
    if timeout is None:
        return None
    return max(timeout, granularity)


class _AdapterBase:
    """Shared preparation / adaptation logic for the sync and async adapters."""

    __slots__ = ("_byo", "_closed", "_core", "_granularity", "_owns", "_proxy", "_timeout")

    def __init__(
        self,
        core: _NativeCore,
        *,
        timeout: float,
        granularity: float,
        byo: _ByoNativeClient | None,
        proxy: _ProxyConfig | None,
    ) -> None:
        self._core = core
        self._timeout = timeout
        self._granularity = granularity
        self._proxy = proxy
        self._byo = byo if byo is not None else _ByoNativeClient()
        self._owns = byo is None
        self._closed = False

    def __repr__(self) -> str:
        """Render the adapter without ever echoing proxy credentials."""
        proxy = self._proxy.redacted_url if self._proxy is not None else None
        return f"{type(self).__name__}(timeout={self._timeout!r}, proxy={proxy!r})"

    def unsupported_proxy_features(self) -> frozenset[str]:
        """Return requested proxy features the adapter cannot honour."""
        return self._proxy.unsupported_features() if self._proxy is not None else frozenset()

    def _prepare(self, request: Request) -> tuple[Headers, bytes]:
        """Resolve content-type, compute framing, drop native-rejected headers."""
        headers = self._resolve_content_type(request)
        headers, body = self._frame(headers, request)
        headers = self._drop_rejected(headers)
        return headers, body

    @staticmethod
    def _resolve_content_type(request: Request) -> Headers:
        """Keep a caller content-type; else derive one from the body."""
        headers = request.headers
        if headers.get("content-type") is not None or request.body is None:
            return headers
        media = request.body.media_type()
        if media is None:
            return headers
        return headers.with_set("Content-Type", str(media))

    def _frame(self, headers: Headers, request: Request) -> tuple[Headers, bytes]:
        """Compute framing headers from the actual body, never trusting caller values."""
        body = request.body
        if body is None:
            headers = headers.without("Content-Length").without("Transfer-Encoding")
            if str(request.method) in _ZERO_LENGTH_METHODS:
                headers = headers.with_set("Content-Length", "0")
            return headers, b""
        # Draining consumes a single-use body exactly once.
        payload = b"".join(body.iter_bytes())
        if body.content_length() >= 0:
            headers = headers.without("Transfer-Encoding").with_set(
                "Content-Length", str(len(payload))
            )
        else:
            headers = headers.without("Content-Length").with_set("Transfer-Encoding", "chunked")
        return headers, payload

    def _drop_rejected(self, headers: Headers) -> Headers:
        """Drop headers the native library rejects; count and log each drop."""
        for name in headers.names():
            if name in self._core.native_rejected:
                headers = headers.without(name)
                self._core.drop_log.record(name)
                _LOGGER.warning("dropping native-rejected request header: %s", name)
        return headers

    def _effective_timeout(self, request: Request) -> float | None:
        """Resolve the per-call timeout, clamped up from any sub-resolution value."""
        requested = request.timeout if request.timeout is not None else self._timeout
        return _clamp_timeout(requested, self._granularity)

    def _record(self, request: Request, headers: Headers, body: bytes) -> None:
        """Record what actually reached the native layer for this attempt."""
        self._core.record_sent(
            SentRequest(
                method=str(request.method),
                url=request.url.wire_form(),
                headers=headers,
                body=body,
                timeout=self._effective_timeout(request),
            )
        )

    @staticmethod
    def _dispatch_error(outcome: Outcome) -> None:
        """Raise the mapped SDK error for a non-response outcome; else return."""
        native_cls = _NATIVE_ERROR_BY_KIND.get(outcome.kind)
        if native_cls is None:
            return
        raise _map_native_error(native_cls())

    @staticmethod
    def _parse_status(raw: int | str) -> Status:
        """Parse the native status; a non-numeric token fails adaptation."""
        try:
            return Status(int(raw))
        except (TypeError, ValueError) as err:
            raise _AdaptationError(f"unparseable status: {raw!r}") from err

    @staticmethod
    def _inbound_headers(spec: ResponseSpec) -> Headers:
        """Build response headers, dropping malformed lines, preserving obs-text."""
        headers = Headers()
        for name, value in spec.headers:
            try:
                headers = headers.with_added(name, value)
            except ValueError:
                # A malformed inbound header is dropped, never fatal to the whole
                # response. Legal-but-unusual obs-text bytes are lenient here and
                # survive unchanged.
                _LOGGER.warning("dropping malformed inbound response header: %r", name)
        return headers


def _inbound_content_length(headers: Headers) -> int:
    """Resolve the response body length, downgrading a malformed value to ``-1``."""
    raw = headers.get("content-length")
    if raw is None:
        return -1
    try:
        return max(0, int(raw))
    except ValueError:
        return -1


def _inbound_media_type(headers: Headers) -> MediaType | None:
    """Parse the response content-type, downgrading a malformed value to ``None``."""
    raw = headers.get("content-type")
    if raw is None:
        return None
    try:
        return MediaType.parse(raw)
    except ValueError:
        return None


def _protocol_of(spec: ResponseSpec) -> Protocol:
    """Map the native protocol token onto the SDK enum, defaulting to HTTP/1.1."""
    try:
        return Protocol.parse(spec.protocol)
    except ValueError:
        return Protocol.HTTP_1_1


class FakeSyncHttpClient(_AdapterBase):
    """In-memory synchronous transport adapter under conformance test."""

    def execute(self, request: Request) -> Response:
        """Send ``request`` once and adapt the programmed native outcome."""
        if self._closed:
            raise ServiceRequestError("FakeSyncHttpClient is closed")
        headers, body = self._prepare(request)
        self._record(request, headers, body)
        outcome = self._core.take()
        self._dispatch_error(outcome)
        assert outcome.response is not None
        native = _SyncNativeResponse(outcome.response)
        self._core.responses.append(native)
        return self._adapt(request, native)

    def _adapt(self, request: Request, native: _SyncNativeResponse) -> Response:
        """Turn a native response into a `Response`; close native on failure."""
        try:
            spec = native.spec
            headers = self._inbound_headers(spec)
            status = self._parse_status(spec.status)
            reader = _SyncNativeReader(native)
            body = ResponseBody.from_stream(
                reader,  # type: ignore[arg-type]  # buffered read/close adapter
                media_type=_inbound_media_type(headers),
                content_length=_inbound_content_length(headers),
            )
            return Response(
                request=request,
                protocol=_protocol_of(spec),
                status=status,
                headers=headers,
                reason=spec.reason,
                body=body,
            )
        except _AdaptationError as err:
            native.close()
            raise ServiceResponseError(str(err), error=err) from err

    def close(self) -> None:
        """Close the adapter; close the underlying client only when owned."""
        if self._closed:
            return
        self._closed = True
        if self._owns:
            self._byo.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class FakeAsyncHttpClient(_AdapterBase):
    """In-memory asynchronous transport adapter under conformance test."""

    async def execute(self, request: Request) -> AsyncResponse:
        """Send ``request`` once and adapt the programmed native outcome.

        A pre-dispatch failure (a closed client) surfaces through the returned
        awaitable — this is an ``async def``, so the raise runs only when the
        coroutine is awaited, never synchronously at call time.
        """
        if self._closed:
            raise ServiceRequestError("FakeAsyncHttpClient is closed")
        headers, body = self._prepare(request)
        self._record(request, headers, body)
        outcome = self._core.take()
        self._dispatch_error(outcome)
        assert outcome.response is not None
        native = _AsyncNativeResponse(outcome.response)
        self._core.responses.append(native)
        return await self._adapt(request, native)

    async def _adapt(self, request: Request, native: _AsyncNativeResponse) -> AsyncResponse:
        """Turn a native response into an `AsyncResponse`; close native on failure."""
        try:
            spec = native.spec
            headers = self._inbound_headers(spec)
            status = self._parse_status(spec.status)
            reader = _AsyncNativeReader(native)
            body = AsyncResponseBody.from_async_stream(
                reader,
                media_type=_inbound_media_type(headers),
                content_length=_inbound_content_length(headers),
            )
            return AsyncResponse(
                request=request,
                protocol=_protocol_of(spec),
                status=status,
                headers=headers,
                reason=spec.reason,
                body=body,
            )
        except _AdaptationError as err:
            await native.close()
            raise ServiceResponseError(str(err), error=err) from err

    async def aclose(self) -> None:
        """Close the adapter; close the underlying client only when owned."""
        if self._closed:
            return
        self._closed = True
        if self._owns:
            self._byo.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


# --------------------------------------------------------------------------- #
# In-memory harnesses that wire the fake adapters to the neutral seam.
# --------------------------------------------------------------------------- #


class _InMemoryHarnessMixin:
    """Shared programming / introspection for the in-memory harnesses."""

    def __init__(self) -> None:
        self._core = _NativeCore()
        self._byo: _ByoNativeClient | None = None

    def expect(self, outcome: Outcome) -> None:
        self._core.program(outcome)

    def sent_requests(self) -> list[SentRequest]:
        return self._core.sent

    def attempt_count(self) -> int:
        return self._core.attempts

    def drop_log(self) -> DropLog:
        return self._core.drop_log

    def native_responses(self) -> list[ClosableNative]:
        return self._core.responses

    def mark_native_rejected(self, *names: str) -> None:
        for name in names:
            self._core.native_rejected.add(name.lower())

    def byo_underlying(self) -> ClosableNative | None:
        return self._byo

    def unsupported_proxy_features(self, client: object) -> frozenset[str]:
        assert isinstance(client, _AdapterBase)
        return client.unsupported_proxy_features()

    def _make_byo(self, use_byo: bool) -> _ByoNativeClient | None:
        if not use_byo:
            return None
        self._byo = _ByoNativeClient()
        return self._byo

    @staticmethod
    def _proxy(options: dict[str, object]) -> _ProxyConfig | None:
        url = options.get("proxy_url")
        if url is None:
            return None
        assert isinstance(url, str)
        features = options.get("proxy_features", frozenset())
        assert isinstance(features, frozenset)
        return _ProxyConfig(url, features)


class InMemorySyncHarness(_InMemoryHarnessMixin, SyncTransportHarness):
    """Synchronous in-memory reference harness."""

    def new_client(self, **options: object) -> HttpClient:
        timeout = options.get("timeout", _DEFAULT_TIMEOUT)
        assert isinstance(timeout, int | float)
        byo = self._make_byo(bool(options.get("byo", False)))
        return FakeSyncHttpClient(
            self._core,
            timeout=float(timeout),
            granularity=self.min_timeout_granularity,
            byo=byo,
            proxy=self._proxy(options),
        )

    def close_client(self, client: HttpClient) -> None:
        """Close a sync adapter via its ``close`` surface."""
        assert isinstance(client, FakeSyncHttpClient)
        client.close()


class InMemoryAsyncHarness(_InMemoryHarnessMixin, AsyncTransportHarness):
    """Asynchronous in-memory reference harness."""

    def new_async_client(self, **options: object) -> AsyncHttpClient:
        timeout = options.get("timeout", _DEFAULT_TIMEOUT)
        assert isinstance(timeout, int | float)
        byo = self._make_byo(bool(options.get("byo", False)))
        return FakeAsyncHttpClient(
            self._core,
            timeout=float(timeout),
            granularity=self.min_timeout_granularity,
            byo=byo,
            proxy=self._proxy(options),
        )

    async def aclose_client(self, client: AsyncHttpClient) -> None:
        """Close an async adapter via its ``aclose`` surface."""
        assert isinstance(client, FakeAsyncHttpClient)
        await client.aclose()


__all__ = [
    "FakeAsyncHttpClient",
    "FakeSyncHttpClient",
    "InMemoryAsyncHarness",
    "InMemorySyncHarness",
]
