# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async ``HttpClient`` implementation built on `httpx.AsyncClient`.

Supports streaming uploads and downloads, per-phase timeouts (connect /
read / write / pool), HTTP/2 (opt-in via `httpx`), and proxies.
Production-grade alternative to the asyncio reference client shipped in
``dexpace-sdk-http-stdlib``.

The transport delegates to `httpx.AsyncClient`. `Request.body` is a
synchronous `RequestBody`, so its ``iter_bytes`` iterator is pumped one
chunk at a time off the event loop via ``asyncio.to_thread`` and fed to
httpx as an async byte stream; file- and stream-backed bodies therefore do
their blocking reads on a worker thread rather than on the loop. When the
body reports a known length the adapter sets ``Content-Length`` so httpx
frames the upload by length instead of ``Transfer-Encoding: chunked``;
unknown-length bodies stay chunked. The response exposes
``AsyncResponseBody.from_async_stream`` wrapping httpx's ``aiter_bytes``
iterator.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from types import TracebackType
from typing import Any, Final, Self

import httpx
from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import http_header_name
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.protocol import Protocol
from dexpace.sdk.core.http.request.request import Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response.async_response import AsyncResponse
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.status import Status

_LOGGER: Final = logging.getLogger(__name__)

_DEFAULT_TIMEOUT: Final[float] = 30.0
_DEFAULT_CHUNK_SIZE: Final[int] = 8192
#: Methods whose semantics call for an explicit zero-length body substitution
#: when the caller supplies no payload (RFC 9110 §8.6).
_ZERO_LENGTH_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH"})


class AsyncHttpxHttpClient:
    """Async transport over `httpx.AsyncClient`.

    Per-phase timeouts (``connect_timeout``, ``read_timeout``,
    ``write_timeout``, ``pool_timeout``) are forwarded to
    `httpx.Timeout`. ``None`` disables the phase's timeout.

    Args:
        connect_timeout: Seconds allowed for connection establishment.
        read_timeout: Seconds allowed between successive reads of the
            response body.
        write_timeout: Seconds allowed between successive writes of the
            request body.
        pool_timeout: Seconds allowed to acquire a connection from the
            pool.
        transport: Optional `httpx.AsyncBaseTransport`; the primary
            extension hook for tests (``httpx.MockTransport``).
        client: Optional pre-built `httpx.AsyncClient` — overrides
            the timeout / transport kwargs entirely. Ownership stays with
            the caller: ``aclose`` does not close a client passed in this
            way, so the caller remains responsible for closing it.

    A client this adapter constructs itself never trusts the process
    environment: `httpx.AsyncClient`'s own ``trust_env`` (which implicitly
    reads ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY``) is pinned to
    ``False``, so proxy routing only ever happens through the SDK's own
    opt-in ``ProxyOptions`` resolution, never silently through httpx's
    defaults. A caller-supplied ``client=`` keeps whatever ``trust_env`` it
    was built with — ownership of that choice stays with the caller.
    """

    __slots__ = ("_client", "_closed", "_owns_client")

    def __init__(
        self,
        *,
        connect_timeout: float | None = _DEFAULT_TIMEOUT,
        read_timeout: float | None = _DEFAULT_TIMEOUT,
        write_timeout: float | None = _DEFAULT_TIMEOUT,
        pool_timeout: float | None = _DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            timeout = httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
            )
            self._client = httpx.AsyncClient(
                timeout=timeout,
                transport=transport,
                follow_redirects=False,
                trust_env=False,
            )
            self._owns_client = True
        self._closed = False

    async def execute(self, request: Request) -> AsyncResponse:
        """Send ``request`` and return the response.

        Raises:
            ServiceRequestError: When the connection cannot be established
                or another non-timeout transport error occurs.
            ServiceRequestTimeoutError: When the connect or write phase
                times out.
            ServiceResponseTimeoutError: When the read phase times out.
        """
        if self._closed:
            raise ServiceRequestError("AsyncHttpxHttpClient is closed")
        httpx_request = self._build_request(request)
        try:
            # ``follow_redirects=False`` is pinned here explicitly rather than
            # left to the client's own default: a caller-supplied (BYO)
            # ``httpx.AsyncClient`` could have been constructed with
            # ``follow_redirects=True``, and redirect handling is the SDK's
            # own pipeline policy's job, never the transport's.
            httpx_response = await self._client.send(
                httpx_request, stream=True, follow_redirects=False
            )
        # Subtype-first: every ``httpx.TimeoutException`` subtype (Connect /
        # Read / Write / Pool) is caught by name *before* the broader
        # ``httpx.ConnectError`` / ``httpx.RequestError`` clauses below, so a
        # timeout is never misclassified as a generic network error by
        # falling through to a wider catch first.
        except httpx.ConnectTimeout as err:
            raise ServiceRequestTimeoutError(str(err), error=err) from err
        except httpx.ReadTimeout as err:
            raise ServiceResponseTimeoutError(str(err), error=err) from err
        except httpx.WriteTimeout as err:
            raise ServiceRequestTimeoutError(str(err), error=err) from err
        except httpx.PoolTimeout as err:
            raise ServiceRequestTimeoutError(str(err), error=err) from err
        except httpx.ConnectError as err:
            raise ServiceRequestError(str(err), error=err) from err
        except httpx.RequestError as err:
            raise ServiceRequestError(str(err), error=err) from err
        return await _build_async_response(request, httpx_response)

    def _build_request(self, request: Request) -> httpx.Request:
        sdk_headers = _resolve_content_type(request.headers, request.body)
        headers = _headers_to_pairs(sdk_headers)
        # ``httpx.AsyncClient`` requires an async byte stream for the request
        # body. ``Request.body`` is a synchronous ``RequestBody``, so its
        # ``iter_bytes`` iterator is pumped one chunk at a time on a worker
        # thread (``asyncio.to_thread``); file/stream reads then block that
        # thread rather than the event loop.
        content: Any = None
        if request.body is not None:
            content = _sync_iter_to_async(request.body.iter_bytes(_DEFAULT_CHUNK_SIZE))
            headers = _resolve_framing_headers(headers, request.body)
        else:
            headers = _strip_framing_headers(headers)
            if str(request.method) in _ZERO_LENGTH_METHODS:
                headers.append((http_header_name.CONTENT_LENGTH.canonical_name, "0"))
        # A per-call ``Request.timeout`` overrides the client's own default
        # for this exchange only; ``None`` defers to whatever the client (or
        # a BYO client's own construction) already has configured.
        timeout = request.timeout if request.timeout is not None else httpx.USE_CLIENT_DEFAULT
        return self._client.build_request(
            method=str(request.method),
            url=request.url.wire_form(),
            headers=headers,
            content=content,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` if owned. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class _AsyncHttpxStreamAdapter:
    """Adapter exposing ``async def read(size) -> bytes`` over httpx's chunks."""

    __slots__ = ("_buffer", "_chunks", "_closed", "_exhausted", "_response")

    def __init__(self, response: httpx.Response, chunk_size: int) -> None:
        self._response = response
        self._chunks = response.aiter_bytes(chunk_size=chunk_size)
        self._buffer = bytearray()
        self._exhausted = False
        self._closed = False

    async def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        if size < 0:
            return await self._read_all()
        while len(self._buffer) < size and not self._exhausted:
            await self._pump()
        if not self._buffer:
            return b""
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    async def _read_all(self) -> bytes:
        while not self._exhausted:
            await self._pump()
        out = bytes(self._buffer)
        self._buffer.clear()
        return out

    async def _pump(self) -> None:
        try:
            chunk = await self._chunks.__anext__()
        except StopAsyncIteration:
            self._exhausted = True
            return
        except httpx.ReadTimeout as err:
            raise ServiceResponseTimeoutError(str(err), error=err) from err
        except httpx.RequestError as err:
            raise ServiceResponseError(str(err), error=err) from err
        self._buffer.extend(chunk)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._response.aclose()


class _AdaptationError(Exception):
    """Raised internally when a native response cannot be adapted."""


def _parse_status(raw: int) -> Status:
    """Parse the native status; a non-numeric token fails adaptation cleanly.

    Status is total over *valid* integers (TRANSPORT-24): an out-of-range
    code (outside 100..599) synthesizes an ``UNKNOWN_<code>`` member rather
    than raising, so a vendor/unusual status still surfaces with its body.
    Only a genuinely unparseable value (not an int at all) fails here.
    """
    try:
        return Status(int(raw))
    except (TypeError, ValueError) as err:
        raise _AdaptationError(f"unparseable status: {raw!r}") from err


def _inbound_headers(httpx_response: httpx.Response) -> Headers:
    """Build response headers, dropping malformed names, preserving obs-text.

    Built one entry at a time rather than in a single ``Headers(...)`` call:
    a defensively-received header whose name fails the RFC 7230 token check
    (a literal space, say) is dropped and logged instead of failing the whole
    response adaptation. A legal-but-unusual obs-text byte in a header
    *value* is tolerated, per `Headers`'s inbound-lenient contract.
    """
    headers = Headers()
    for name, value in httpx_response.headers.multi_items():
        try:
            headers = headers.with_added(name, value)
        except ValueError:
            _LOGGER.warning("dropping malformed inbound response header: %r", name)
    return headers


async def _build_async_response(request: Request, httpx_response: httpx.Response) -> AsyncResponse:
    try:
        status = _parse_status(httpx_response.status_code)
        headers = _inbound_headers(httpx_response)
    except _AdaptationError as err:
        await httpx_response.aclose()
        raise ServiceResponseError(str(err), error=err) from err
    stream = _AsyncHttpxStreamAdapter(httpx_response, _DEFAULT_CHUNK_SIZE)
    content_length = _content_length(httpx_response)
    body = AsyncResponseBody.from_async_stream(stream, content_length=content_length)
    return AsyncResponse(
        request=request,
        protocol=_protocol(httpx_response),
        status=status,
        headers=headers,
        reason=httpx_response.reason_phrase or None,
        body=body,
    )


def _protocol(httpx_response: httpx.Response) -> Protocol:
    """Map httpx's reported HTTP version onto core's `Protocol` enum.

    Args:
        httpx_response: The streamed httpx response.

    Returns:
        The negotiated protocol, or `Protocol.HTTP_1_1` when httpx does not
        report a recognizable version.
    """
    try:
        return Protocol.parse(httpx_response.http_version)
    except ValueError:
        return Protocol.HTTP_1_1


def _content_length(httpx_response: httpx.Response) -> int:
    # The stream yields decompressed bytes, so a Content-Length that describes
    # the compressed payload would misreport the decoded length. Omit it when
    # the response is content-encoded.
    if httpx_response.headers.get(http_header_name.CONTENT_ENCODING.value):
        return -1
    raw = httpx_response.headers.get(http_header_name.CONTENT_LENGTH.value)
    if raw is None:
        return -1
    try:
        return max(0, int(raw))
    except ValueError:
        return -1


def _resolve_content_type(headers: Headers, body: RequestBody | None) -> Headers:
    """Keep a caller-set ``Content-Type``; else derive one from the body.

    Args:
        headers: The SDK request headers.
        body: The outgoing request body, if any.

    Returns:
        ``headers`` unchanged when the caller already set a content-type (or
        there is no body / the body reports none); otherwise ``headers`` with
        the body's `MediaType` stamped on as ``Content-Type``.
    """
    if headers.get(http_header_name.CONTENT_TYPE) is not None or body is None:
        return headers
    media = body.media_type()
    if media is None:
        return headers
    return headers.with_set(http_header_name.CONTENT_TYPE, str(media))


def _resolve_framing_headers(
    pairs: list[tuple[str, str]], body: RequestBody
) -> list[tuple[str, str]]:
    """Recompute framing headers from the actual body; never trust the caller.

    httpx receives the body as a bare iterator with no declared length, so it
    falls back to ``Transfer-Encoding: chunked`` unless told otherwise. A
    caller-supplied ``Content-Length`` that disagrees with the body actually
    being written (or one left over from a different, unknown-length body) is
    stale and would violate RFC 9112 §6.1's "not both" framing rule, so any
    existing ``Content-Length`` / ``Transfer-Encoding`` is dropped first and
    the correct one is derived from ``body.content_length()``.

    Args:
        pairs: The header name/value pairs forwarded to httpx.
        body: The outgoing request body.

    Returns:
        ``pairs`` with framing headers recomputed from ``body``.
    """
    pairs = _strip_framing_headers(pairs)
    length = body.content_length()
    if length >= 0:
        pairs.append((http_header_name.CONTENT_LENGTH.canonical_name, str(length)))
    return pairs


def _strip_framing_headers(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop any caller-supplied ``Content-Length`` / ``Transfer-Encoding``.

    Framing headers describe a body; when there is none, any stale
    caller-supplied value would misdescribe the (absent) payload, so both are
    dropped here before the bodyless-method case re-adds an explicit
    ``Content-Length: 0`` (RFC 9110 §8.6).

    Args:
        pairs: The header name/value pairs forwarded to httpx.

    Returns:
        A new list with any ``content-length`` / ``transfer-encoding`` entries
        removed.
    """
    return [(n, v) for n, v in pairs if n.lower() not in ("content-length", "transfer-encoding")]


_ITER_DONE: Final = object()


def _next_chunk(source: Iterator[bytes]) -> bytes | object:
    """Pull the next chunk, returning the `_ITER_DONE` sentinel at the end.

    `StopIteration` cannot propagate across an `asyncio.to_thread` boundary,
    so exhaustion is signalled with a sentinel instead.

    Args:
        source: The synchronous bytes iterator being drained.

    Returns:
        The next chunk, or `_ITER_DONE` when the iterator is exhausted.
    """
    try:
        return next(source)
    except StopIteration:
        return _ITER_DONE


async def _sync_iter_to_async(source: Iterator[bytes]) -> AsyncIterator[bytes]:
    """Adapt a synchronous bytes iterator into an async iterator.

    Each ``next(source)`` call runs on a worker thread via
    ``asyncio.to_thread`` so a blocking file/stream read does not stall the
    event loop.

    Args:
        source: The synchronous bytes iterator to pump.

    Yields:
        Each chunk produced by `source`, in order.
    """
    while True:
        chunk = await asyncio.to_thread(_next_chunk, source)
        if chunk is _ITER_DONE:
            return
        assert isinstance(chunk, bytes)
        yield chunk


def _headers_to_pairs(headers: Headers) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for name, values in headers.items():
        for value in values:
            pairs.append((name, value))
    return pairs


__all__ = ["AsyncHttpxHttpClient"]
