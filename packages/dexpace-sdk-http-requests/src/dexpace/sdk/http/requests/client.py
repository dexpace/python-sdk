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
        self._session = session if session is not None else requests.Session()
        self._closed = False

    def execute(self, request: Request) -> Response:
        """Send ``request`` and return the response.

        An in-range HTTP status the server returns is preserved on the
        response, even when it is not a named member of the registry; only a
        status outside the valid 100-599 range is treated as a protocol error.

        Raises:
            ServiceRequestError: When the request cannot be dispatched
                (connection refused, DNS failure, generic transport error).
            ServiceRequestTimeoutError: On ``ConnectTimeout``.
            ServiceResponseTimeoutError: On ``ReadTimeout``.
            ServiceResponseError: When the status code is outside the valid
                HTTP range (100-599).
        """
        if self._closed:
            raise ServiceRequestError("RequestsHttpClient is closed")
        data = _body_payload(request.body)
        headers = {name: ", ".join(values) for name, values in request.headers.items()}
        if not (data is None or isinstance(data, _SizedBody)):
            # Unknown-length streaming body: requests frames it with
            # ``Transfer-Encoding: chunked``. Drop any caller-supplied
            # ``Content-Length`` so the wire never carries both framing headers
            # (RFC 9112 §6.1) over an un-chunk-framed body.
            headers = {n: v for n, v in headers.items() if n.lower() != "content-length"}
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
        ServiceResponseError: When the status code is outside the valid HTTP
            range (100-599). The response handle is released first.
    """
    try:
        status = Status(raw.status_code)
    except ValueError as err:
        raw.close()
        raise ServiceResponseError(f"Invalid status code: {raw.status_code}", error=err) from err
    headers = Headers(_header_pairs(raw))
    content_length = _content_length(headers)
    body = ResponseBody.from_stream(_IterContentStream(raw), content_length=content_length)  # type: ignore[arg-type]
    reason: str | None = raw.reason if raw.reason else None
    return Response(
        request=request,
        protocol=_protocol_of(raw),
        status=status,
        headers=headers,
        reason=reason,
        body=body,
    )


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
