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
- **A body with no caller- or body-derived Content-Type still gets one.**
  When neither the caller nor ``RequestBody.media_type()`` supplies a
  Content-Type, ``urllib.request`` itself inserts its own legacy default
  (``application/x-www-form-urlencoded``, mirroring HTML form-submission
  semantics) once the request reaches ``do_request_``. This adapter does not
  suppress that fallback — doing so would mean bypassing
  ``urllib.request.Request`` entirely. Supply a body with an explicit media
  type (or set ``Content-Type`` on the request) to avoid it.
- **A malformed inbound header name can take later headers with it.**
  ``http.client`` parses response headers via ``email.message.Message``,
  whose own header parser treats a line that is not a valid ``name: value``
  token (e.g. a name containing a space) as the end of the header block —
  every header line that follows is silently discarded along with it, before
  this adapter's own (per-entry, defensive) header conversion ever sees them.
  This adapter still drops the one malformed entry it *is* handed rather than
  aborting the whole conversion, but it cannot recover headers ``http.client``
  itself already lost. Not reproducible by bypassing ``http.client`` without
  abandoning ``urllib.request`` entirely, so it is documented here instead.

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

import http.client
import logging
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

_LOGGER: Final = logging.getLogger("dexpace.sdk.http.stdlib.urllib")

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

        Exception-mapping matrix (native cause -> SDK type):

        - Connection refused / DNS failure (``URLError`` wrapping a
          non-timeout ``OSError``) -> `ServiceRequestError`.
        - Connect-phase timeout (``URLError(reason=TimeoutError)``) ->
          `ServiceRequestTimeoutError`. Discriminated from a bare
          ``TimeoutError`` by *type* (``isinstance(err.reason,
          TimeoutError)``), never by message text — this is deliberate and
          explicit, not an incidental reliance on Python 3.10+ aliasing
          ``socket.timeout`` to ``TimeoutError``.
        - Read-phase timeout (a *bare* ``TimeoutError``, not wrapped in
          ``URLError``) -> `ServiceResponseTimeoutError`.
        - A garbled/unparseable response (``http.client.HTTPException``,
          e.g. ``BadStatusLine`` — never wrapped in ``URLError`` by urllib) ->
          `ServiceRequestError`.
        - A read-phase failure on the response body itself (stall, ``os``
          truncation) is mapped by `_ReadMappingStream`, not here.

        Raises:
            ServiceRequestError: When the connection cannot be established,
                or the response is too garbled to interpret at all.
            ServiceRequestTimeoutError: When the connection times out before
                the request is transmitted (connect phase).
            ServiceResponseError: When reading the response body fails.
            ServiceResponseTimeoutError: When the request was transmitted but
                the response read times out (read phase).
        """
        if self._closed:
            raise ServiceRequestError("UrllibHttpClient is closed")
        timeout = request.timeout if request.timeout is not None else self.timeout
        raw = _build_urllib_request(request)
        try:
            opened = _OPENER.open(raw, timeout=timeout)
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
        except http.client.HTTPException as err:
            # A garbled/unparseable response (bad status line, connection
            # dropped before any status line arrived) is not wrapped in
            # ``URLError`` by urllib — ``http.client`` raises its own
            # exception hierarchy for these directly out of ``getresponse()``.
            # The exchange never yielded an interpretable response, so this
            # maps to the same retryable bucket as a connection failure
            # rather than escaping as a raw stdlib exception.
            raise ServiceRequestError(str(err), error=err) from err
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
    headers, body_bytes = _prepare(request)
    # ``urllib.request.Request`` accepts a ``Mapping[str, str]`` only, so
    # multi-value headers are joined into a single comma-separated string
    # rather than dropped. Safe for most list-typed headers (``Accept``,
    # ``Cache-Control``); wire-incorrect for headers that legitimately
    # repeat (``Set-Cookie`` — not applicable on outbound; ``WWW-Authenticate``
    # in proxy/forwarder scenarios). See module docstring.
    flattened = {name: ", ".join(values) for name, values in headers.items()}
    return _UrllibRequest(
        url=request.url.wire_form(),
        data=body_bytes,
        headers=flattened,
        method=str(request.method),
    )


def _prepare(request: Request) -> tuple[Headers, bytes | None]:
    """Resolve Content-Type and drop any stale framing headers.

    A caller-set ``Content-Type`` is authoritative; otherwise the body's own
    ``media_type()`` is used when present. Any pre-existing ``Content-Length``
    / ``Transfer-Encoding`` is dropped rather than trusted — ``http.client``
    recomputes ``Content-Length`` itself from the exact bytes handed to it
    (see ``HTTPConnection._get_content_length``), including the RFC 9110
    §8.6 zero-length default for body-bearing methods sent with no payload.
    Trusting a caller-supplied value that disagrees with the real payload
    would break HTTP framing: the peer reads short or hangs waiting for
    bytes that never arrive.

    Args:
        request: The request being adapted.

    Returns:
        The resolved headers and the fully-drained body bytes (``None`` when
        the request carries no body).
    """
    headers = _resolve_content_type(request).without("Content-Length").without("Transfer-Encoding")
    if request.body is None:
        return headers, None
    return headers, b"".join(request.body.iter_bytes())


def _resolve_content_type(request: Request) -> Headers:
    """Keep a caller-set Content-Type; else derive one from the body."""
    headers = request.headers
    if headers.get("Content-Type") is not None or request.body is None:
        return headers
    media = request.body.media_type()
    if media is None:
        return headers
    return headers.with_set("Content-Type", str(media))


def _build_response(request: Request, opened: object) -> Response:
    status_code: int = getattr(opened, "status", 200)
    # ``Status`` is total over integers (TRANSPORT-24): a code outside the
    # registered 100..599 band synthesizes an ``UNKNOWN_<code>`` member rather
    # than raising, so a vendor/unusual status still surfaces with its body.
    status = Status(status_code)
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

    ``http.client`` does not decode ``Content-Encoding`` (gzip/deflate): the
    stream yields the bytes exactly as framed on the wire, so a present
    ``Content-Length`` matches the body this transport exposes and is reported
    as-is — even under ``Content-Encoding``. (Transports that decompress
    transparently, e.g. requests/httpx/aiohttp, must instead drop the header
    there, since it would then describe the compressed payload rather than the
    decoded stream they hand back.)

    Args:
        headers: The response headers.

    Returns:
        The body length in bytes, or ``-1`` when absent or unparseable.
    """
    raw = headers.get("Content-Length")
    if raw is None:
        return -1
    try:
        return max(0, int(raw))
    except ValueError:
        return -1


def _convert_headers(raw: object) -> Headers:
    """Convert urllib's parsed response headers, dropping malformed names.

    ``http.client``/``email`` header parsing already tolerates obs-text in
    values, but a name `Headers` rejects (e.g. one containing a space) must
    still be dropped rather than aborting the whole conversion — entries are
    added one at a time so a single bad name never takes down the good ones
    that follow it, mirroring the defensive inbound-header handling every
    adapter in this SDK provides. (A name malformed enough to confuse
    ``http.client``'s own header parser may already have been dropped, or
    have taken subsequent lines with it, upstream of this function — that is
    a limitation of building on ``http.client``, not of this conversion.)

    Args:
        raw: The native header container (an ``email.message.Message``-like
            object), or ``None``.

    Returns:
        The converted, lenient `Headers`.
    """
    if raw is None:
        return Headers()
    items_method = getattr(raw, "items", None)
    if items_method is None:
        return Headers()
    headers = Headers()
    for name, value in items_method():
        try:
            headers = headers.with_added(name, value)
        except ValueError:
            _LOGGER.warning("dropping malformed inbound response header: %r", name)
    return headers


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
