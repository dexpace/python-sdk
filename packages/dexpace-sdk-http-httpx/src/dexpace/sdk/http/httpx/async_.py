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

_DEFAULT_TIMEOUT: Final[float] = 30.0
_DEFAULT_CHUNK_SIZE: Final[int] = 8192


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
            self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
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
            httpx_response = await self._client.send(httpx_request, stream=True)
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
        headers = _headers_to_pairs(request.headers)
        # ``httpx.AsyncClient`` requires an async byte stream for the request
        # body. ``Request.body`` is a synchronous ``RequestBody``, so its
        # ``iter_bytes`` iterator is pumped one chunk at a time on a worker
        # thread (``asyncio.to_thread``); file/stream reads then block that
        # thread rather than the event loop.
        content: Any = None
        if request.body is not None:
            content = _sync_iter_to_async(request.body.iter_bytes(_DEFAULT_CHUNK_SIZE))
            _frame_known_length(headers, request.headers, request.body)
        return self._client.build_request(
            method=str(request.method),
            url=request.url.wire_form(),
            headers=headers,
            content=content,
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


async def _build_async_response(request: Request, httpx_response: httpx.Response) -> AsyncResponse:
    try:
        status = Status(int(httpx_response.status_code))
    except ValueError as err:
        # Genuinely invalid status (outside 100..599). Release the underlying
        # socket back to the pool; httpx async responses reject sync ``close``,
        # so use ``aclose``.
        await httpx_response.aclose()
        raise ServiceResponseError(
            f"Invalid status code: {httpx_response.status_code}", error=err
        ) from err
    headers = Headers(httpx_response.headers.multi_items())
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


def _frame_known_length(pairs: list[tuple[str, str]], headers: Headers, body: RequestBody) -> None:
    """Add a ``Content-Length`` pair for a known-length body.

    httpx receives the body as a bare iterator with no declared length, so it
    falls back to ``Transfer-Encoding: chunked``. Declaring the length lets it
    frame the upload by length instead. Unknown-length bodies (``-1``) are left
    chunked, and an explicit caller-supplied ``Content-Length`` is preserved.

    Args:
        pairs: The header name/value pairs forwarded to httpx; mutated in place.
        headers: The SDK request headers, consulted case-insensitively.
        body: The outgoing request body.
    """
    length = body.content_length()
    if length < 0 or http_header_name.CONTENT_LENGTH in headers:
        return
    pairs.append((http_header_name.CONTENT_LENGTH.canonical_name, str(length)))


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
