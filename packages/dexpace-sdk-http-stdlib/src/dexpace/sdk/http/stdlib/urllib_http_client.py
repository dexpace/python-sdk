# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Synchronous reference ``HttpClient`` implementation built on ``urllib.request``.

Not for production traffic — it is the example/test transport that ships
with ``core``. Production deployments should plug in an adapter built on a
real HTTP library (httpx, requests, aiohttp) instead.

Limitations of the underlying ``urllib.request`` transport:

- **No streaming uploads.** The request body is fully buffered into memory
  via ``b"".join(request.body.iter_bytes())`` before send — ``urllib.request``
  does not support chunked transfer-encoding for outbound payloads. For
  streaming uploads, plug in an alternative transport (``httpx``,
  ``aiohttp``, ``requests`` with ``stream=True`` semantics).
- **Coarse timeouts.** A single ``timeout`` value covers both connection
  establishment and read; ``urllib.request`` offers no separate
  connect/read/write granularity. Production transports (``httpx``,
  ``aiohttp``) expose per-phase timeouts.
- **Multi-value request headers are flattened.** ``urllib.request.Request``
  accepts only a ``Mapping[str, str]``, so multiple values for the same
  header name are joined with ``", "``. This is correct for most list-typed
  headers (``Accept``, ``Cache-Control``) but wire-incorrect for headers
  that legitimately repeat — notably ``Set-Cookie`` (not applicable on
  outbound) and, in proxy/forwarder scenarios, ``WWW-Authenticate``. Use a
  production transport if you need to emit repeated outbound headers.

Redirects are **not** followed by this transport. A custom opener disables
``urllib``'s built-in ``HTTPRedirectHandler`` so 3xx responses surface to
the pipeline as ordinary ``Response`` objects, letting the redirect policy
own hop limits, the method matrix, and cross-origin credential stripping —
matching the other adapters (all of which disable library-level redirects).

The response body is exposed as a `ResponseBody.from_stream` wrapper over a
read-mapping adapter so streaming reads on the response side are possible
and read-phase failures (stalls, truncation) surface as SDK errors. Maps
urllib's exception types into the SDK error hierarchy.
"""

from __future__ import annotations

import contextlib
import http.client
from collections.abc import Mapping
from http.client import HTTPResponse
from types import TracebackType
from typing import Final, Self
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, OpenerDirector, build_opener
from urllib.request import Request as _UrllibRequest

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

# ``http.client.HTTPResponse.version`` is an integer: ``10`` for HTTP/1.0 and
# ``11`` for HTTP/1.1. Map it onto the SDK's protocol enum; default elsewhere.
_PROTOCOL_BY_VERSION: Final[Mapping[int, Protocol]] = {
    10: Protocol.HTTP_1_0,
    11: Protocol.HTTP_1_1,
}


class _NoRedirectHandler(HTTPRedirectHandler):
    """Redirect handler that refuses to follow 3xx responses.

    Returning ``None`` from ``redirect_request`` tells ``urllib`` not to
    reissue the request, so the 3xx surfaces as an ``HTTPError`` that the
    ``execute`` path converts into a normal ``Response``. This keeps redirect
    handling (hop caps, the method matrix, cross-origin credential stripping)
    in the pipeline's redirect policy rather than in the transport.
    """

    def redirect_request(
        self,
        req: _UrllibRequest,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _build_no_redirect_opener() -> OpenerDirector:
    """Build an opener whose redirect handler does not follow 3xx responses."""
    return build_opener(_NoRedirectHandler)


_OPENER: Final[OpenerDirector] = _build_no_redirect_opener()


class UrllibHttpClient:
    """Reference synchronous transport over ``urllib.request``.

    Behaves as a structural ``HttpClient`` (single ``execute`` method) plus
    a context-manager surface so a ``Pipeline`` can take ownership and call
    ``close`` on exit. The implementation prepares an ``urllib.Request`` per
    call, streams the response into a buffered ``ResponseBody``, and maps
    urllib failure modes into the SDK error hierarchy.

    Redirects are not followed at the transport layer (a private opener
    disables ``urllib``'s ``HTTPRedirectHandler``), so 3xx responses reach
    the pipeline intact.

    See the module docstring for the full list of underlying ``urllib``
    limitations (no streaming uploads, coarse timeouts, multi-value
    request-header flattening).

    Attributes:
        timeout: Single timeout in seconds applied to ``urlopen``; covers
            both connect and read with no per-phase granularity.
    """

    __slots__ = ("_closed", "timeout")

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.timeout = timeout
        self._closed = False

    def execute(self, request: Request) -> Response:
        """Send ``request`` and return the response.

        3xx responses are returned as-is (redirects are not followed at the
        transport layer); the pipeline's redirect policy decides whether to
        reissue.

        Raises:
            ServiceRequestError: When the connection cannot be established.
            ServiceRequestTimeoutError: When the connection times out before
                the request is transmitted (connect phase).
            ServiceResponseError: When reading the response fails.
            ServiceResponseTimeoutError: When the request was transmitted but
                the response read times out (read phase).
        """
        if self._closed:
            raise ServiceRequestError("UrllibHttpClient is closed")
        raw = _build_urllib_request(request)
        try:
            opened = _OPENER.open(raw, timeout=self.timeout)
        except HTTPError as err:
            # urllib raises HTTPError for 3xx (redirects are not followed) and
            # 4xx/5xx; surface them via the normal Response path so policies
            # (redirect, retry, error_map) can react.
            return _build_response(request, err)
        except URLError as err:
            # Since 3.10 ``socket.timeout`` is ``TimeoutError``. A connect
            # timeout reaches us wrapped as ``URLError(reason=TimeoutError)``
            # — the request never left, so it is a *request* timeout.
            if isinstance(err.reason, TimeoutError):
                raise ServiceRequestTimeoutError(str(err), error=err) from err
            raise ServiceRequestError(str(err), error=err) from err
        except TimeoutError as err:
            # A *bare* ``TimeoutError`` (not wrapped in ``URLError``) is a
            # read-phase timeout: the request was transmitted but the response
            # stalled. Classify as a *response* timeout so non-idempotent
            # reads are not auto-retried as if they never reached the service.
            raise ServiceResponseTimeoutError(str(err), error=err) from err
        return _build_response(request, opened)

    def close(self) -> None:
        """Mark the client as closed. Subsequent calls raise."""
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _build_urllib_request(request: Request) -> _UrllibRequest:
    body_bytes: bytes | None = None
    if request.body is not None:
        body_bytes = b"".join(request.body.iter_bytes())
    # ``urllib.request.Request`` accepts a ``Mapping[str, str]`` only, so
    # multi-value headers are joined into a single comma-separated string
    # rather than dropped. Safe for most list-typed headers (``Accept``,
    # ``Cache-Control``); wire-incorrect for headers that legitimately
    # repeat (``Set-Cookie`` — not applicable on outbound; ``WWW-Authenticate``
    # in proxy/forwarder scenarios). See module docstring.
    headers = {name: ", ".join(values) for name, values in request.headers.items()}
    return _UrllibRequest(
        url=request.url.wire_form(),
        data=body_bytes,
        headers=headers,
        method=str(request.method),
    )


def _build_response(request: Request, opened: object) -> Response:
    status_code: int = getattr(opened, "status", 200)
    try:
        status = Status(status_code)
    except ValueError as err:
        # Only a genuinely out-of-range code (outside 100..599) reaches here:
        # ``Status`` synthesizes a member for any in-range code. Release the
        # underlying response first so the connection is not leaked.
        _close_quietly(opened)
        raise ServiceResponseError(f"Unknown status code: {status_code}", error=err) from err
    raw_headers = getattr(opened, "headers", None)
    headers = _convert_headers(raw_headers)
    content_length = _body_content_length(headers)
    body = ResponseBody.from_stream(
        _ReadMappingStream(opened),  # type: ignore[arg-type]  # adapter satisfies BinaryIO
        content_length=content_length,
    )
    reason = getattr(opened, "reason", None)
    return Response(
        request=request,
        protocol=_protocol_of(opened),
        status=status,
        headers=headers,
        reason=reason,
        body=body,
    )


def _protocol_of(opened: object) -> Protocol:
    """Map ``http.client.HTTPResponse.version`` onto the SDK protocol enum.

    Args:
        opened: The urllib response (or ``HTTPError``) handle.

    Returns:
        The reported protocol, or ``Protocol.HTTP_1_1`` when unknown.
    """
    version = getattr(opened, "version", None)
    if isinstance(version, int):
        return _PROTOCOL_BY_VERSION.get(version, Protocol.HTTP_1_1)
    return Protocol.HTTP_1_1


def _body_content_length(headers: Headers) -> int:
    """Resolve the body length to advertise on the ``ResponseBody``.

    Returns ``-1`` (unknown) when ``Content-Encoding`` is present, because
    the stream yields decompressed bytes and the upstream ``Content-Length``
    counts the compressed payload — propagating it would lie about the body.

    Args:
        headers: The response headers.

    Returns:
        The body length in bytes, or ``-1`` when unknown or unreliable.
    """
    if "content-encoding" in headers:
        return -1
    raw = headers.get("Content-Length")
    if raw is None:
        return -1
    try:
        return max(0, int(raw))
    except ValueError:
        return -1


def _close_quietly(opened: object) -> None:
    """Close ``opened`` if it exposes ``close``, swallowing any error.

    Used on the failure path before raising so a partially constructed
    response does not leak its underlying connection.

    Args:
        opened: The urllib response (or ``HTTPError``) to release.
    """
    close = getattr(opened, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def _convert_headers(raw: object) -> Headers:
    if raw is None:
        return Headers()
    items_method = getattr(raw, "items", None)
    if items_method is None:
        return Headers()
    return Headers(list(items_method()))


class _ReadMappingStream:
    """``BinaryIO``-shaped adapter that maps read failures to SDK errors.

    ``ResponseBody.from_stream`` calls ``read(size)`` and ``close()`` on its
    argument. The raw ``http.client.HTTPResponse`` raises bare
    ``TimeoutError`` on a stalled read and ``http.client.IncompleteRead`` on
    a truncated body — neither is an ``SdkError``. This adapter forwards
    ``read``/``close`` to the underlying response while translating
    read-phase failures: the request is already on the wire, so a stall is a
    ``ServiceResponseTimeoutError`` and truncation/other transport failures
    are ``ServiceResponseError``.
    """

    __slots__ = ("_closed", "_response")

    def __init__(self, response: HTTPResponse) -> None:
        self._response = response
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        try:
            if size < 0:
                return self._response.read()
            return self._response.read(size)
        except TimeoutError as err:
            raise ServiceResponseTimeoutError("Response body read timed out", error=err) from err
        except http.client.IncompleteRead as err:
            raise ServiceResponseError(f"Response body truncated: {err}", error=err) from err
        except OSError as err:
            raise ServiceResponseError(f"Response body read failed: {err}", error=err) from err

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._response.close()


__all__ = ["UrllibHttpClient"]
