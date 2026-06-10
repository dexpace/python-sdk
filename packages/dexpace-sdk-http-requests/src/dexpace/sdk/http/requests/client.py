# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Synchronous ``HttpClient`` implementation built on the ``requests`` library.

``RequestsHttpClient`` wraps a ``requests.Session`` configured with
``stream=True`` so response bodies are read lazily and surfaced through the
SDK's `ResponseBody` streaming API. Request bodies are produced via
`RequestBody.iter_bytes` in 8 KiB chunks.

Exception mapping (``requests`` -> SDK):

- ``requests.ConnectTimeout`` -> `ServiceRequestTimeoutError`
- ``requests.ReadTimeout`` -> `ServiceResponseTimeoutError`
- ``requests.ConnectionError`` -> `ServiceRequestError`
- ``requests.RequestException`` (catch-all) -> `ServiceRequestError`

Failures that surface later, while the response body is being streamed, are
classified as response-side errors (the request was already sent): a
``requests.Timeout`` -> `ServiceResponseTimeoutError` and any other
``requests.RequestException`` -> `ServiceResponseError`.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import TracebackType
from typing import TYPE_CHECKING, Final, Self

import requests
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

if TYPE_CHECKING:
    from dexpace.sdk.core.http.request.request_body import RequestBody

_DEFAULT_TIMEOUT: Final[float] = 30.0
_CHUNK_SIZE: Final[int] = 8192


class RequestsHttpClient:
    """Synchronous transport over ``requests.Session``.

    Behaves as a structural ``HttpClient`` (single ``execute`` method) plus
    a context-manager surface so a ``Pipeline`` can take ownership and call
    ``close`` on exit. Each call sends one ``requests`` request with
    ``stream=True`` and wraps the streamed response into a
    `ResponseBody`.

    Attributes:
        timeout: Single timeout in seconds applied to ``Session.request``;
            covers both connect and read with no per-phase granularity.
            Use ``timeout=(connect, read)`` semantics via a custom session
            if finer-grained control is needed.
    """

    __slots__ = ("_closed", "_session", "timeout")

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._closed = False

    def execute(self, request: Request) -> Response:
        """Send ``request`` and return the response.

        Raises:
            ServiceRequestError: When the request cannot be dispatched
                (connection refused, DNS failure, generic transport error).
            ServiceRequestTimeoutError: On ``ConnectTimeout``.
            ServiceResponseTimeoutError: On ``ReadTimeout``.
            ServiceResponseError: When the status code is outside the
                known IANA registry.
        """
        if self._closed:
            raise ServiceRequestError("RequestsHttpClient is closed")
        data = _body_iterator(request.body)
        headers = {name: ", ".join(values) for name, values in request.headers.items()}
        # ``requests`` sends an iterator as chunked transfer-encoding by
        # default. If the body length is known, surface it as
        # ``Content-Length`` so transports / servers that do not handle
        # chunked uploads still see a framed request.
        if request.body is not None and "content-length" not in {k.lower() for k in headers}:
            length = request.body.content_length()
            if length >= 0:
                headers["Content-Length"] = str(length)
        try:
            raw = self._session.request(
                method=str(request.method),
                url=request.url.wire_form(),
                headers=headers,
                data=data,
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
            )
        except requests.ConnectTimeout as err:
            raise ServiceRequestTimeoutError(str(err), error=err) from err
        except requests.ReadTimeout as err:
            raise ServiceResponseTimeoutError(str(err), error=err) from err
        except requests.ConnectionError as err:
            raise ServiceRequestError(str(err), error=err) from err
        except requests.RequestException as err:
            raise ServiceRequestError(str(err), error=err) from err
        return _build_response(request, raw)

    def close(self) -> None:
        """Mark the client as closed and release the underlying session."""
        if self._closed:
            return
        self._closed = True
        self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _body_iterator(body: RequestBody | None) -> Iterator[bytes] | None:
    if body is None:
        return None
    return body.iter_bytes(_CHUNK_SIZE)


def _build_response(request: Request, raw: requests.Response) -> Response:
    try:
        status = Status(raw.status_code)
    except ValueError as err:
        raw.close()
        raise ServiceResponseError(f"Unknown status code: {raw.status_code}", error=err) from err
    headers = Headers(list(raw.headers.items()))
    body = ResponseBody.from_stream(_IterContentStream(raw))  # type: ignore[arg-type]
    reason: str | None = raw.reason if raw.reason else None
    return Response(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=status,
        headers=headers,
        reason=reason,
        body=body,
    )


class _IterContentStream:
    """Adapter that exposes ``requests.Response.iter_content`` as a stream.

    `ResponseBody.from_stream` calls ``read(chunk_size)`` and
    ``close()`` on its argument. ``requests`` doesn't expose a file-like
    object that honours chunk-size hints once decoded, but ``iter_content``
    does — this adapter buffers what the iterator yields and serves it in
    arbitrarily-sized ``read`` requests.
    """

    __slots__ = ("_buf", "_closed", "_iter", "_response")

    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self._iter: Iterator[bytes] | None = None
        self._buf = bytearray()
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        if self._iter is None:
            self._iter = self._response.iter_content(chunk_size=_CHUNK_SIZE)
        if size < 0:
            while (chunk := self._next_chunk()) is not None:
                if chunk:
                    self._buf.extend(chunk)
            out = bytes(self._buf)
            self._buf.clear()
            return out
        while len(self._buf) < size:
            chunk = self._next_chunk()
            if chunk is None:
                break
            if chunk:
                self._buf.extend(chunk)
        if not self._buf:
            return b""
        take = min(size, len(self._buf))
        out = bytes(self._buf[:take])
        del self._buf[:take]
        return out

    def _next_chunk(self) -> bytes | None:
        """Pull the next body chunk, mapping read-phase failures to SDK errors.

        Returns the chunk, or ``None`` at end of stream. The request is already
        on the wire, so a read-phase failure is a response-side error: a
        ``requests`` read timeout becomes ``ServiceResponseTimeoutError`` and
        any other transport failure mid-body becomes ``ServiceResponseError``.
        """
        assert self._iter is not None
        try:
            return next(self._iter)
        except StopIteration:
            return None
        except requests.Timeout as err:
            raise ServiceResponseTimeoutError("Response body read timed out", error=err) from err
        except requests.RequestException as err:
            raise ServiceResponseError(f"Response body read failed: {err}", error=err) from err

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._response.close()


__all__ = ["RequestsHttpClient"]
