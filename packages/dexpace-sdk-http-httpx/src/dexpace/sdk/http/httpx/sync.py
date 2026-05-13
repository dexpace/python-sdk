"""Synchronous ``HttpClient`` implementation built on :mod:`httpx`.

Supports streaming uploads and downloads, per-phase timeouts (connect /
read / write / pool), HTTP/2 (opt-in via :mod:`httpx`), and proxies.
Production-grade alternative to the urllib reference client shipped in
``dexpace-sdk-http-stdlib``.

The transport delegates to :class:`httpx.Client`, streaming the request
body via :meth:`RequestBody.iter_bytes` and exposing the response as a
:class:`ResponseBody` whose ``iter_bytes`` walks the httpx response's own
``iter_bytes`` iterator. Closing the SDK response closes the underlying
httpx response and releases the connection back to the pool.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import TracebackType
from typing import Any, Final, Self

import httpx
from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.protocol import Protocol
from dexpace.sdk.core.http.request.request import Request
from dexpace.sdk.core.http.response.response import Response
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.http.response.status import Status

_DEFAULT_TIMEOUT: Final[float] = 30.0
_DEFAULT_CHUNK_SIZE: Final[int] = 8192


class HttpxHttpClient:
    """Synchronous transport over :class:`httpx.Client`.

    Behaves as a structural ``HttpClient`` (single ``execute`` method) plus
    a context-manager surface so a ``Pipeline`` can take ownership and call
    ``close`` on exit.

    Per-phase timeouts (``connect_timeout``, ``read_timeout``,
    ``write_timeout``, ``pool_timeout``) are forwarded to
    :class:`httpx.Timeout`. ``None`` disables the phase's timeout.

    Args:
        connect_timeout: Seconds allowed for connection establishment.
        read_timeout: Seconds allowed between successive reads of the
            response body.
        write_timeout: Seconds allowed between successive writes of the
            request body.
        pool_timeout: Seconds allowed to acquire a connection from the
            pool.
        transport: Optional :class:`httpx.BaseTransport`; the primary
            extension hook for tests (``httpx.MockTransport``).
        client: Optional pre-built :class:`httpx.Client` — overrides the
            timeout / transport kwargs entirely. Ownership transfers to
            this transport.
    """

    __slots__ = ("_client", "_closed", "_owns_client")

    def __init__(
        self,
        *,
        connect_timeout: float | None = _DEFAULT_TIMEOUT,
        read_timeout: float | None = _DEFAULT_TIMEOUT,
        write_timeout: float | None = _DEFAULT_TIMEOUT,
        pool_timeout: float | None = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
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
            self._client = httpx.Client(timeout=timeout, transport=transport)
            self._owns_client = True
        self._closed = False

    def execute(self, request: Request) -> Response:
        """Send ``request`` and return the response.

        Raises:
            ServiceRequestError: When the connection cannot be established
                or another non-timeout transport error occurs.
            ServiceRequestTimeoutError: When the connect or write phase
                times out.
            ServiceResponseTimeoutError: When the read phase times out.
        """
        if self._closed:
            raise ServiceRequestError("HttpxHttpClient is closed")
        httpx_request = self._build_request(request)
        try:
            httpx_response = self._client.send(httpx_request, stream=True)
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
        return _build_response(request, httpx_response)

    def _build_request(self, request: Request) -> httpx.Request:
        headers = _headers_to_pairs(request.headers)
        content: Iterator[bytes] | None = (
            request.body.iter_bytes(_DEFAULT_CHUNK_SIZE) if request.body is not None else None
        )
        return self._client.build_request(
            method=str(request.method),
            url=request.url.wire_form(),
            headers=headers,
            content=content,
        )

    def close(self) -> None:
        """Close the underlying ``httpx.Client`` if owned. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class _HttpxStreamAdapter:
    """Adapter exposing ``read(size) -> bytes`` over ``httpx.Response.iter_bytes``.

    Wraps the chunk iterator behind a tiny pull-based interface so it can
    plug into ``ResponseBody.from_stream``, which expects something
    BinaryIO-shaped. The adapter buffers the trailing slice of each chunk
    when the consumer asks for a smaller ``size`` than the chunk yields.
    """

    __slots__ = ("_buffer", "_chunks", "_closed", "_exhausted", "_response")

    def __init__(self, response: httpx.Response, chunk_size: int) -> None:
        self._response = response
        self._chunks = response.iter_bytes(chunk_size=chunk_size)
        self._buffer = bytearray()
        self._exhausted = False
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        if size < 0:
            return self._read_all()
        while len(self._buffer) < size and not self._exhausted:
            self._pump()
        if not self._buffer:
            return b""
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def _read_all(self) -> bytes:
        while not self._exhausted:
            self._pump()
        out = bytes(self._buffer)
        self._buffer.clear()
        return out

    def _pump(self) -> None:
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._exhausted = True
            return
        except httpx.ReadTimeout as err:
            raise ServiceResponseTimeoutError(str(err), error=err) from err
        except httpx.RequestError as err:
            raise ServiceResponseError(str(err), error=err) from err
        self._buffer.extend(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._response.close()


def _build_response(request: Request, httpx_response: httpx.Response) -> Response:
    try:
        status = Status(int(httpx_response.status_code))
    except ValueError as err:
        httpx_response.close()
        raise ServiceResponseError(
            f"Unknown status code: {httpx_response.status_code}", error=err
        ) from err
    headers = Headers(httpx_response.headers.multi_items())
    stream = _HttpxStreamAdapter(httpx_response, _DEFAULT_CHUNK_SIZE)
    content_length = _content_length(httpx_response)
    body: Any = ResponseBody.from_stream(stream, content_length=content_length)  # type: ignore[arg-type]
    return Response(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=status,
        headers=headers,
        reason=httpx_response.reason_phrase or None,
        body=body,
    )


def _content_length(httpx_response: httpx.Response) -> int:
    raw = httpx_response.headers.get("content-length")
    if raw is None:
        return -1
    try:
        return max(0, int(raw))
    except ValueError:
        return -1


def _headers_to_pairs(headers: Headers) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for name, values in headers.items():
        for value in values:
            pairs.append((name, value))
    return pairs


__all__ = ["HttpxHttpClient"]
