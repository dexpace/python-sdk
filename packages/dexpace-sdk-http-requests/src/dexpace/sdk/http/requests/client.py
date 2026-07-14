# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Synchronous ``HttpClient`` implementation built on the ``requests`` library.

``RequestsHttpClient`` wraps a ``requests.Session`` configured with
``stream=True`` so response bodies are read lazily and surfaced through the
SDK's `ResponseBody` streaming API.

Request-body framing follows the rule a recipient can parse unambiguously:

- A body whose length is known (replayable or not) is wrapped in a sized view
  exposing ``__len__``, so ``requests`` sets ``Content-Length`` itself and
  frames the upload by length while still streaming it chunk-by-chunk — the
  payload is never buffered into memory. The adapter never injects
  ``Content-Length`` next to a bare generator, which would make ``requests``
  add ``Transfer-Encoding: chunked`` while ``HTTPAdapter`` still sent the body
  un-framed (RFC 9112 forbids carrying both headers).
- A body of unknown length is passed as an iterator and ``requests`` applies
  ``Transfer-Encoding: chunked`` cleanly; the adapter drops any caller-set
  ``Content-Length`` on this path so the two framing headers are never sent
  together.

Either way the wire request carries exactly one framing header, never both.

Exception mapping (``requests`` -> SDK):

- ``requests.ConnectTimeout`` -> `ServiceRequestTimeoutError`
- ``requests.ReadTimeout`` -> `ServiceResponseTimeoutError`
- ``requests.ConnectionError`` -> `ServiceRequestError`
- ``InterruptedError`` (a blocking call aborted by an external signal) ->
  `RequestCancelledError`
- ``requests.RequestException`` (catch-all) -> `ServiceRequestError`

Failures that surface later, while the response body is being streamed, are
classified as response-side errors (the request was already sent): a
``requests.Timeout`` -> `ServiceResponseTimeoutError` and any other
``requests.RequestException`` -> `ServiceResponseError`. A native response
that cannot be adapted (an unparseable status, most commonly) closes the raw
``requests`` response before raising `ServiceResponseError`, so a failed
adaptation never leaks the connection.

Two dangerous ``requests`` / ``urllib3`` defaults are neutralised on any
session this adapter creates for itself: ``urllib3`` retries connect-phase
errors unless told otherwise, and ``requests.Session.trust_env`` implicitly
merges ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``.netrc`` from the environment. A
session the adapter creates is given a zero-retry ``HTTPAdapter`` on both
``http://`` and ``https://`` and ``trust_env=False``, matching the SDK's
opt-in-only proxy-resolution rule (`dexpace.sdk.core.util.proxy`). A
caller-supplied (BYO) session is never reconfigured this way — its own mounted
adapters and ``trust_env`` are left exactly as the caller set them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import TracebackType
from typing import TYPE_CHECKING, Final, Self

import requests
from dexpace.sdk.core.errors import (
    RequestCancelledError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.media_type import MediaType
from dexpace.sdk.core.http.common.protocol import Protocol
from dexpace.sdk.core.http.request.request import Request
from dexpace.sdk.core.http.response.response import Response
from dexpace.sdk.core.http.response.response_body import ResponseBody
from dexpace.sdk.core.http.response.status import Status

if TYPE_CHECKING:
    from dexpace.sdk.core.http.request.request_body import RequestBody

_LOGGER: Final = logging.getLogger("dexpace.sdk.http.requests")

_DEFAULT_TIMEOUT: Final[float] = 30.0
_CHUNK_SIZE: Final[int] = 8192


class _AdaptationError(Exception):
    """Raised internally when a native response cannot be adapted."""


def _new_owned_session() -> requests.Session:
    """Build a session this adapter owns, pinned to safe defaults.

    A session the adapter creates itself gets two explicit hardenings that a
    caller-supplied (BYO) session is never forced into:

    - Zero transport-level retries on both schemes. ``urllib3`` retries
      connect-phase failures by default; the SDK's own retry policy is the
      only thing that may re-send a request, so the transport must never also
      retry underneath it.
    - ``trust_env=False``. ``requests`` otherwise reads ``HTTP_PROXY`` /
      ``HTTPS_PROXY`` / ``.netrc`` from the environment implicitly, which
      conflicts with the SDK's opt-in-only proxy-resolution rule.

    A BYO session keeps whatever the caller configured — including its own
    mounted adapters and ``trust_env`` — untouched; mutating a session the
    caller owns would be surprising and could break unrelated code sharing it.

    Returns:
        A fresh, hardened ``requests.Session``.
    """
    session = requests.Session()
    session.trust_env = False
    no_retry = requests.adapters.HTTPAdapter(max_retries=0)
    session.mount("http://", no_retry)
    session.mount("https://", no_retry)
    return session


# urllib3 reports the negotiated HTTP version as an int on the raw response
# (``11`` == HTTP/1.1, ``10`` == HTTP/1.0, ``20`` == HTTP/2). Map the ones we
# can name onto the core ``Protocol`` enum; anything else defaults to HTTP/1.1.
_PROTOCOL_BY_VERSION: Final[dict[int, Protocol]] = {
    10: Protocol.HTTP_1_0,
    11: Protocol.HTTP_1_1,
    20: Protocol.HTTP_2,
}


class RequestsHttpClient:
    """Synchronous transport over ``requests.Session``.

    Behaves as a structural ``HttpClient`` (single ``execute`` method) plus
    a context-manager surface so a ``Pipeline`` can take ownership and call
    ``close`` on exit. Each call sends one ``requests`` request with
    ``stream=True`` and wraps the streamed response into a
    `ResponseBody`.

    A caller-supplied ``session`` is treated as borrowed: ``close`` leaves it
    open so other components sharing the pooled session keep working. Only a
    session this client created is closed on ``close``.

    Attributes:
        timeout: Single timeout in seconds applied to ``Session.request``;
            covers both connect and read with no per-phase granularity.
            Use ``timeout=(connect, read)`` semantics via a custom session
            if finer-grained control is needed.
    """

    __slots__ = ("_closed", "_owns_session", "_session", "timeout")

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.timeout = timeout
        self._owns_session = session is None
        self._session = session if session is not None else _new_owned_session()
        self._closed = False

    def execute(self, request: Request) -> Response:
        """Send ``request`` and return the response.

        Any HTTP status the server returns is preserved on the response, in
        or out of the registered 100-599 range and whether or not it is a
        named member of the registry (TRANSPORT-24) — a vendor or unusual
        status is never treated as a protocol error.

        Raises:
            ServiceRequestError: When the request cannot be dispatched
                (connection refused, DNS failure, generic transport error).
            ServiceRequestTimeoutError: On ``ConnectTimeout``.
            ServiceResponseTimeoutError: On ``ReadTimeout``.
            RequestCancelledError: On an ``InterruptedError`` (the blocking
                call was aborted by an external signal).
            ServiceResponseError: When a response was received but could not
                be adapted (for example, an unparseable status).
        """
        if self._closed:
            raise ServiceRequestError("RequestsHttpClient is closed")
        data = _body_payload(request.body)
        headers = _prepare_headers(request, data)
        # A per-call timeout on the ``Request`` overrides the client's default;
        # neither is clamped further since ``requests``/``urllib3`` timeouts are
        # plain floats with no meaningful minimum resolution to truncate to.
        timeout = request.timeout if request.timeout is not None else self.timeout
        try:
            raw = self._session.request(
                method=str(request.method),
                url=request.url.wire_form(),
                headers=headers,
                data=data,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
        except requests.ConnectTimeout as err:
            raise ServiceRequestTimeoutError(str(err), error=err) from err
        except requests.ReadTimeout as err:
            raise ServiceResponseTimeoutError(str(err), error=err) from err
        except requests.ConnectionError as err:
            raise ServiceRequestError(str(err), error=err) from err
        except InterruptedError as err:
            raise RequestCancelledError(str(err), error=err) from err
        except requests.RequestException as err:
            raise ServiceRequestError(str(err), error=err) from err
        try:
            return _build_response(request, raw)
        except _AdaptationError as err:
            raw.close()
            raise ServiceResponseError(str(err), error=err) from err

    def close(self) -> None:
        """Mark the client as closed; close the session only when owned."""
        if self._closed:
            return
        self._closed = True
        if self._owns_session:
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


def _prepare_headers(request: Request, data: _SizedBody | Iterator[bytes] | None) -> dict[str, str]:
    """Resolve the outbound headers ``requests`` should send for ``request``.

    A caller-set ``Content-Type`` is authoritative; otherwise one is derived
    from the body's own `MediaType`, so a JSON/form/multipart body gets the
    right content-type without every caller having to set it by hand. An
    unknown-length streaming body drops any caller-set ``Content-Length`` so
    the wire never carries both framing headers (RFC 9112 §6.1) once
    ``requests`` adds ``Transfer-Encoding: chunked``.

    Args:
        request: The originating SDK request.
        data: The payload already resolved by `_body_payload`.

    Returns:
        The single-valued header mapping ``requests`` should send.
    """
    resolved = request.headers
    if resolved.get("content-type") is None and request.body is not None:
        media = request.body.media_type()
        if media is not None:
            resolved = resolved.with_set("Content-Type", str(media))
    headers = {name: ", ".join(values) for name, values in resolved.items()}
    if not (data is None or isinstance(data, _SizedBody)):
        headers = {n: v for n, v in headers.items() if n.lower() != "content-length"}
    return headers


def _body_payload(body: RequestBody | None) -> _SizedBody | Iterator[bytes] | None:
    """Render ``body`` into the payload ``requests`` should send.

    A body whose length is known is wrapped in a `_SizedBody`, so ``requests``
    frames it with ``Content-Length`` and streams it without buffering the
    whole payload. A body of unknown length is returned as a chunk iterator,
    which ``requests`` sends with ``Transfer-Encoding: chunked``. This keeps
    the two framing headers mutually exclusive on the wire.

    Args:
        body: The request body, or ``None`` for a body-less request.

    Returns:
        ``None`` for no body, a `_SizedBody` for a known-length body, or a
        chunk iterator for an unknown-length body.
    """
    if body is None:
        return None
    length = body.content_length()
    if length >= 0:
        return _SizedBody(body, length)
    return body.iter_bytes(_CHUNK_SIZE)


class _SizedBody:
    """Length-framed, streaming view over a request body for ``requests``.

    Exposes ``__len__`` so ``requests`` sets ``Content-Length`` instead of
    falling back to ``Transfer-Encoding: chunked``, and ``__iter__`` so the
    body is streamed chunk-by-chunk rather than materialised into memory. A
    multi-gigabyte `FileRequestBody` therefore goes out framed by length
    without being read into RAM.
    """

    __slots__ = ("_body", "_length")

    def __init__(self, body: RequestBody, length: int) -> None:
        self._body = body
        self._length = length

    def __len__(self) -> int:
        return self._length

    def __iter__(self) -> Iterator[bytes]:
        return self._body.iter_bytes(_CHUNK_SIZE)


def _build_response(request: Request, raw: requests.Response) -> Response:
    """Wrap a streamed ``requests`` response into an SDK `Response`.

    Args:
        request: The originating SDK request, echoed back on the response.
        raw: The streamed ``requests`` response (``stream=True``).

    Returns:
        The constructed `Response`, with the body left unread.

    Raises:
        _AdaptationError: If the native status cannot be parsed as an integer.
            The caller is responsible for closing ``raw`` on this path.
    """
    status = _parse_status(raw.status_code)
    headers = _inbound_headers(raw)
    content_length = _content_length(headers)
    media_type = _inbound_media_type(headers)
    body = ResponseBody.from_stream(
        _IterContentStream(raw),  # type: ignore[arg-type]
        media_type=media_type,
        content_length=content_length,
    )
    reason: str | None = raw.reason if raw.reason else None
    return Response(
        request=request,
        protocol=_protocol_of(raw),
        status=status,
        headers=headers,
        reason=reason,
        body=body,
    )


def _parse_status(raw_status: object) -> Status:
    """Parse the native status; a non-numeric token fails adaptation.

    Status is total over integers (TRANSPORT-24): an out-of-range code
    (outside 100..599) synthesizes an ``UNKNOWN_<code>`` member rather than
    raising, so a vendor/unusual status still surfaces with its body. Only a
    genuinely unparseable value (never produced by a real HTTP status line,
    but reachable through a synthesized native response) fails adaptation.

    Args:
        raw_status: The native ``status_code`` value.

    Returns:
        The parsed `Status`.

    Raises:
        _AdaptationError: If ``raw_status`` cannot be parsed as an integer.
    """
    try:
        return Status(int(raw_status))  # type: ignore[call-overload]
    except (TypeError, ValueError) as err:
        raise _AdaptationError(f"unparseable status: {raw_status!r}") from err


def _inbound_headers(raw: requests.Response) -> Headers:
    """Build response headers defensively, dropping malformed lines.

    A malformed inbound header name (one the SDK's `Headers` token check
    rejects) is dropped and logged rather than failing the whole response —
    the native response has already been received, so one bad line should
    never discard an otherwise-usable body. Legal-but-unusual obs-text bytes
    are lenient here and survive unchanged.

    Args:
        raw: The streamed ``requests`` response.

    Returns:
        The parsed `Headers`, with any malformed lines dropped.
    """
    headers = Headers()
    for name, value in _header_pairs(raw):
        try:
            headers = headers.with_added(name, value)
        except ValueError:
            _LOGGER.warning("dropping malformed inbound response header: %r", name)
    return headers


def _header_pairs(raw: requests.Response) -> list[tuple[str, str]]:
    """Return response headers as ``(name, value)`` pairs preserving repeats.

    ``requests.Response.headers`` is a ``CaseInsensitiveDict`` that comma-joins
    duplicate header lines, which corrupts repeated headers such as
    ``Set-Cookie``. The underlying urllib3 ``HTTPHeaderDict`` (``raw.raw``)
    keeps each line distinct, so read from it when available.

    Args:
        raw: The streamed ``requests`` response.

    Returns:
        The header lines as a list of ``(name, value)`` tuples.
    """
    underlying = getattr(raw.raw, "headers", None)
    if underlying is not None:
        return list(underlying.items())
    return list(raw.headers.items())


def _inbound_media_type(headers: Headers) -> MediaType | None:
    """Parse the response content-type, downgrading a malformed value to ``None``.

    Args:
        headers: The parsed response headers.

    Returns:
        The parsed `MediaType`, or ``None`` when absent or unparseable.
    """
    raw = headers.get("content-type")
    if raw is None:
        return None
    try:
        return MediaType.parse(raw)
    except ValueError:
        return None


def _content_length(headers: Headers) -> int:
    """Return the framed body length, or ``-1`` when it is unknown or unusable.

    ``requests`` transparently decompresses the body when the response carries
    ``Content-Encoding``, so the upstream ``Content-Length`` describes the
    compressed bytes and no longer matches the decoded stream the SDK exposes.
    In that case the length is dropped.

    Args:
        headers: The parsed response headers.

    Returns:
        The non-negative content length, or ``-1`` when absent, unparseable,
        or invalidated by a ``Content-Encoding`` header.
    """
    if "content-encoding" in headers:
        return -1
    raw = headers.get("content-length")
    if raw is None:
        return -1
    try:
        return max(0, int(raw))
    except ValueError:
        return -1


def _protocol_of(raw: requests.Response) -> Protocol:
    """Map the negotiated HTTP version onto the core ``Protocol`` enum.

    Args:
        raw: The streamed ``requests`` response.

    Returns:
        The matching ``Protocol`` member, defaulting to ``HTTP_1_1`` when the
        version is missing or unrecognised.
    """
    version = getattr(raw.raw, "version", None)
    if isinstance(version, int):
        return _PROTOCOL_BY_VERSION.get(version, Protocol.HTTP_1_1)
    return Protocol.HTTP_1_1


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
