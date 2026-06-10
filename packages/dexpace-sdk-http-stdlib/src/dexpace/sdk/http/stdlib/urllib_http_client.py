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

The response body is exposed as a `ResponseBody.from_stream` wrapper
so streaming reads on the response side are possible. Maps urllib's
exception types into the SDK error hierarchy.
"""

from __future__ import annotations

import contextlib
from types import TracebackType
from typing import Final, Self
from urllib.error import HTTPError, URLError
from urllib.request import Request as _UrllibRequest
from urllib.request import urlopen

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


class UrllibHttpClient:
    """Reference synchronous transport over ``urllib.request``.

    Behaves as a structural ``HttpClient`` (single ``execute`` method) plus
    a context-manager surface so a ``Pipeline`` can take ownership and call
    ``close`` on exit. The implementation prepares an ``urllib.Request`` per
    call, streams the response into a buffered ``ResponseBody``, and maps
    urllib failure modes into the SDK error hierarchy.

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
            opened = urlopen(raw, timeout=self.timeout)
        except HTTPError as err:
            # urllib raises HTTPError for 4xx/5xx; surface them via the
            # normal Response path so policies (retry, error_map) can react.
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
        # A valid-but-unregistered code raises here before the body wraps the
        # stream. Release the underlying response first so the connection is
        # not leaked (parity with the aiohttp/httpx adapters).
        _close_quietly(opened)
        raise ServiceResponseError(f"Unknown status code: {status_code}", error=err) from err
    raw_headers = getattr(opened, "headers", None)
    headers = _convert_headers(raw_headers)
    body = ResponseBody.from_stream(opened)  # type: ignore[arg-type]  # urllib's HTTPResponse satisfies BinaryIO
    reason = getattr(opened, "reason", None)
    return Response(
        request=request,
        protocol=Protocol.HTTP_1_1,
        status=status,
        headers=headers,
        reason=reason,
        body=body,
    )


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


__all__ = ["UrllibHttpClient"]
