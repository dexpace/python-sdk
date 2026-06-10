# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""``AsyncHttpClient`` implementation backed by `aiohttp`.

``aiohttp`` exposes only an async API; this package therefore ships an
async transport without a sync twin. The adapter is a thin pass-through:

- Request bodies are forwarded to ``aiohttp`` via an async-iterable shim
  over `RequestBody.iter_bytes`, so uploads stream without buffering
  the full payload into memory.
- Response content streams through ``aiohttp.StreamReader``; we wrap it as
  an `AsyncResponseBody` so the SDK's body lifecycle (deferred
  read, deterministic close) is preserved.
- Transport exceptions are mapped to the SDK's typed error hierarchy.

For sync callers, use ``dexpace-sdk-http-stdlib``'s ``UrllibHttpClient`` or
``dexpace-sdk-http-requests``'s ``RequestsHttpClient``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import TracebackType
from typing import TYPE_CHECKING, Final, Self

import aiohttp
from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.protocol import Protocol
from dexpace.sdk.core.http.response.async_response import AsyncResponse
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.status import Status

if TYPE_CHECKING:
    from dexpace.sdk.core.http.request.request import Request
    from dexpace.sdk.core.http.request.request_body import RequestBody

_DEFAULT_TIMEOUT: Final[float] = 30.0
_UPLOAD_CHUNK: Final[int] = 8192


class AiohttpHttpClient:
    """Async ``HttpClient`` over an `aiohttp.ClientSession`.

    The client owns the session by default and releases it on ``aclose``.
    Pass an existing ``session`` to share connection pooling with other
    components; the caller is then responsible for closing it.

    Attributes:
        timeout: Per-phase request timeout in seconds, applied to both the
            connect and the socket-read phases via
            ``aiohttp.ClientTimeout(sock_connect=..., sock_read=...)`` so the
            two phases raise distinguishable exceptions. ``None`` disables the
            timeout entirely (not recommended).
    """

    __slots__ = ("_closed", "_owns_session", "_session", "timeout")

    def __init__(
        self,
        *,
        timeout: float | None = _DEFAULT_TIMEOUT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if timeout is not None and timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.timeout = timeout
        self._session = session
        self._owns_session = session is None
        self._closed = False

    async def execute(self, request: Request) -> AsyncResponse:
        if self._closed:
            raise ServiceRequestError("AiohttpHttpClient is closed")
        session = await self._ensure_session()
        # Per-phase budgets (not a single ``total=``) so aiohttp raises the
        # distinguishable ``ConnectionTimeoutError`` for a connect-phase stall
        # and ``SocketTimeoutError`` for a read-phase stall. This keeps the
        # connect -> request-error / read -> response-error split consistent
        # with the other transports, which all use per-operation timeouts.
        timeout_cfg = (
            aiohttp.ClientTimeout(sock_connect=self.timeout, sock_read=self.timeout)
            if self.timeout is not None
            else None
        )
        data = _payload(request.body)
        try:
            ctx = session.request(
                method=str(request.method),
                url=request.url.wire_form(),
                headers=_request_headers(request.headers),
                data=data,
                timeout=timeout_cfg,
                allow_redirects=False,
            )
            aio_response = await ctx
        except aiohttp.ClientConnectorError as err:
            raise ServiceRequestError(f"Connect failed: {err}", error=err) from err
        except aiohttp.ConnectionTimeoutError as err:
            raise ServiceRequestTimeoutError(
                f"Connection to {request.url} timed out", error=err
            ) from err
        except TimeoutError as err:
            raise ServiceResponseTimeoutError(
                f"Request to {request.url} timed out", error=err
            ) from err
        except aiohttp.ClientResponseError as err:
            raise ServiceResponseError(f"Response error: {err}", error=err) from err
        except aiohttp.ClientError as err:
            raise ServiceRequestError(f"Transport error: {err}", error=err) from err
        return _wrap_response(request, aio_response)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> Self:
        await self._ensure_session()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            # Construct lazily so the session binds the running event loop.
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session


def _request_headers(headers: Headers) -> list[tuple[str, str]]:
    """Flatten ``Headers`` to a list of ``(name, value)`` pairs.

    aiohttp accepts the multi-pair form natively, which preserves repeated
    header names (``Set-Cookie``, ``Via``).
    """
    out: list[tuple[str, str]] = []
    for name, values in headers.items():
        for value in values:
            out.append((name, value))
    return out


def _payload(body: RequestBody | None) -> AsyncIterator[bytes] | None:
    """Adapt a sync ``RequestBody`` to an async iterator for aiohttp.

    aiohttp accepts async iterables for streaming uploads. We wrap
    ``iter_bytes(8192)`` so chunks are yielded one at a time without
    pre-buffering the full payload.
    """
    if body is None:
        return None
    return _aiter_body(body)


async def _aiter_body(body: RequestBody) -> AsyncIterator[bytes]:
    for chunk in body.iter_bytes(_UPLOAD_CHUNK):
        if chunk:
            yield chunk


def _wrap_response(request: Request, aio_response: aiohttp.ClientResponse) -> AsyncResponse:
    try:
        status = Status(aio_response.status)
    except ValueError as err:
        # Release the handle before bailing so the connection returns to the
        # pool instead of leaking (aiohttp's release() is synchronous).
        aio_response.release()
        raise ServiceResponseError(
            f"Unknown status code: {aio_response.status}", error=err
        ) from err
    headers = Headers(tuple(aio_response.headers.items()))
    reason = aio_response.reason
    content_length = _content_length(aio_response)
    body = AsyncResponseBody.from_async_stream(
        _StreamReaderAdapter(aio_response), content_length=content_length
    )
    return AsyncResponse(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=status,
        headers=headers,
        reason=reason,
        body=body,
    )


class _StreamReaderAdapter:
    """``SupportsAsyncRead`` adapter over an `aiohttp.ClientResponse`.

    Owns the response handle; closing the adapter releases the connection
    back to the pool.
    """

    __slots__ = ("_closed", "_response")

    def __init__(self, response: aiohttp.ClientResponse) -> None:
        self._response = response
        self._closed = False

    async def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        try:
            if size < 0:
                return await self._response.content.read()
            return await self._response.content.read(size)
        except TimeoutError as err:
            raise ServiceResponseTimeoutError("Response body read timed out", error=err) from err
        except aiohttp.ClientError as err:
            raise ServiceResponseError(f"Response body read failed: {err}", error=err) from err

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._response.release()


def _content_length(aio_response: aiohttp.ClientResponse) -> int:
    """Extract ``Content-Length`` from the response, or ``-1`` when absent/invalid."""
    raw = aio_response.headers.get("Content-Length")
    if raw is None:
        return -1
    try:
        return max(0, int(raw))
    except ValueError:
        return -1


__all__ = ["AiohttpHttpClient"]
