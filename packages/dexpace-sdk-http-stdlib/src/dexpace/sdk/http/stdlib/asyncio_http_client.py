# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Async reference ``HttpClient`` built on ``asyncio.open_connection``.

A minimal HTTP/1.1 client that uses raw sockets (no third-party deps).
Intended for tests, examples, and demonstrating the async pipeline shape;
production-quality async transports should come from adapters built on
``httpx`` / ``aiohttp``.

The implementation handles only:

- HTTP/1.1
- Plain TCP (``http://``); TLS (``https://``) via ``ssl.create_default_context``
- ``Content-Length``-framed and connection-close-framed responses.
  ``Transfer-Encoding: chunked`` responses are rejected loudly with a
  ``ServiceResponseError`` rather than silently truncated — the adapter
  forces ``Connection: close``, so a body without ``Content-Length`` is read
  to EOF instead of being fabricated as empty.
- Connection: close (one request per connection)

Additional limitations that mirror `urllib_http_client`:

- **No streaming uploads.** The request body is fully buffered into memory
  before send (the blocking iteration runs off the event loop via
  ``asyncio.to_thread`` so the loop is not stalled). For streaming uploads,
  plug in an alternative transport (``httpx``, ``aiohttp``).
- **Coarse timeouts.** A single ``timeout`` value is applied to connect,
  status-line read, header read, and body read. Production transports
  expose per-phase granularity. A connect-phase timeout surfaces as
  ``ServiceRequestTimeoutError``; a timeout during any read phase
  (status line, headers, or body) surfaces as
  ``ServiceResponseTimeoutError``.
- **Multi-value request headers are emitted as repeated lines** (one
  ``Name: Value`` line per value), which is the wire-correct form on the
  async side — note this differs from the urllib reference client, which
  flattens to ``", "``-joined values because ``urllib.request.Request``
  only accepts ``Mapping[str, str]``.

These limits keep the reference implementation small enough to verify by
inspection. For anything else, plug in a proper adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl as _ssl
from collections.abc import Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Final, Self

from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.protocol import Protocol
from dexpace.sdk.core.http.common.url import Url
from dexpace.sdk.core.http.request.request import Request
from dexpace.sdk.core.http.response.async_response import AsyncResponse
from dexpace.sdk.core.http.response.async_response_body import AsyncResponseBody
from dexpace.sdk.core.http.response.status import Status

if TYPE_CHECKING:
    from dexpace.sdk.core.http.request.request_body import RequestBody

_DEFAULT_TIMEOUT: Final[float] = 30.0
_CRLF: Final[bytes] = b"\r\n"
_BODY_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH"})

# Map the HTTP version token from the status line (``HTTP/1.1``) onto the
# SDK's protocol enum. Default to HTTP/1.1 when the token is unrecognized.
_PROTOCOL_BY_VERSION: Final[Mapping[str, Protocol]] = {
    "HTTP/1.0": Protocol.HTTP_1_0,
    "HTTP/1.1": Protocol.HTTP_1_1,
}


class AsyncioHttpClient:
    """Reference async HTTP/1.1 client.

    See the module docstring for the full list of limitations (no streaming
    uploads, coarse single-phase timeout, etc).

    Attributes:
        timeout: Single timeout in seconds applied to every I/O phase
            (connect, status-line read, header read, body read). No
            per-phase granularity.
        ssl_context: Optional pre-built ``SSLContext`` for ``https://``.
            Defaults to ``ssl.create_default_context()`` on first use.
    """

    __slots__ = ("_closed", "_ssl_context", "timeout")

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        ssl_context: _ssl.SSLContext | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.timeout = timeout
        self._ssl_context = ssl_context
        self._closed = False

    async def execute(self, request: Request) -> AsyncResponse:
        if self._closed:
            raise ServiceRequestError("AsyncioHttpClient is closed")
        host, port, secure, path = _split_url(request.url)
        reader, writer = await self._open(host, port, secure)
        try:
            await self._send(writer, request, host, port, secure, path)
            return await self._read(reader, request)
        except TimeoutError as err:
            # The connection was established (``_open`` already maps connect
            # timeouts), so a timeout here is a read/write-phase stall.
            raise ServiceResponseTimeoutError(
                f"Read from {host}:{port} timed out", error=err
            ) from err
        except (asyncio.IncompleteReadError, ValueError) as err:
            # Mid-body drop (``readexactly``) or an over-limit line
            # (``readline`` re-raises ``LimitOverrunError`` as ``ValueError``).
            raise ServiceResponseError(
                f"Reading response from {host}:{port} failed: {err}", error=err
            ) from err
        except ServiceResponseError:
            raise
        except OSError as err:
            # A mid-exchange socket error after a successful connect is a
            # response-side failure, not a connect failure: the request may
            # already be on the wire, so it is deliberately classified as a
            # response error (not retry-safe) rather than the request error the
            # httpx / aiohttp / requests adapters use for their generic
            # transport bucket. The conservative choice avoids blind-retrying a
            # potentially-received non-idempotent request.
            raise ServiceResponseError(
                f"Exchange with {host}:{port} failed: {err}", error=err
            ) from err
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def aclose(self) -> None:
        self._closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _open(
        self,
        host: str,
        port: int,
        secure: bool,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # ``http://`` must never attempt a TLS handshake, even when a caller
        # supplied an ``ssl_context`` — the ``secure`` guard wraps the whole
        # expression so a plaintext request stays plaintext.
        ssl_ctx = (self._ssl_context or _ssl.create_default_context()) if secure else None
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_ctx),
                timeout=self.timeout,
            )
        except TimeoutError as err:
            raise ServiceRequestTimeoutError(
                f"Connect to {host}:{port} timed out", error=err
            ) from err
        except OSError as err:
            raise ServiceRequestError(f"Connect to {host}:{port} failed: {err}", error=err) from err

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        request: Request,
        host: str,
        port: int,
        secure: bool,
        path: str,
    ) -> None:
        body_bytes = b""
        if request.body is not None:
            # Materialising the body iterates a possibly-blocking sync source
            # (file / stream reads); run it off the event loop so the loop is
            # not stalled while the payload is gathered.
            body_bytes = await asyncio.to_thread(_drain_body, request.body)
        headers = Headers(request.headers.items())
        if "host" not in headers:
            # Preserve a caller-supplied Host (virtual hosting); only derive
            # it from the URL when the request did not set one. RFC 9112 §3.2
            # requires the port for non-default ports.
            headers = headers.with_set("Host", _host_header(host, port, secure))
        if "content-length" not in headers:
            if body_bytes:
                headers = headers.with_set("Content-Length", str(len(body_bytes)))
            elif str(request.method) in _BODY_METHODS:
                # RFC 9110 §8.6 recommends an explicit ``Content-Length: 0``
                # for body-bearing methods sent without a payload.
                headers = headers.with_set("Content-Length", "0")
        headers = headers.with_set("Connection", "close")
        request_line = f"{request.method} {path or '/'} HTTP/1.1".encode()
        lines: list[bytes] = [request_line]
        for name, values in headers.items():
            for value in values:
                lines.append(f"{name}: {value}".encode("latin-1"))
        lines.extend((b"", body_bytes))
        writer.write(_CRLF.join(lines))
        await writer.drain()

    async def _read(
        self,
        reader: asyncio.StreamReader,
        request: Request,
    ) -> AsyncResponse:
        status_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
        if not status_line:
            raise ServiceResponseError("Empty response from server")
        parts = status_line.decode("latin-1").rstrip("\r\n").split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ServiceResponseError(f"Malformed status line: {status_line!r}")
        protocol = _PROTOCOL_BY_VERSION.get(parts[0], Protocol.HTTP_1_1)
        status = _parse_status(parts[1])
        reason = (parts[2] or None) if len(parts) > 2 else None
        headers = await self._read_headers(reader)
        body_bytes = await self._read_body(reader, headers)
        return AsyncResponse(
            request=request,
            protocol=protocol,
            status=status,
            headers=headers,
            reason=reason,
            body=AsyncResponseBody.from_bytes(body_bytes),
        )

    async def _read_body(self, reader: asyncio.StreamReader, headers: Headers) -> bytes:
        """Read the response body, honouring the response's framing.

        ``Transfer-Encoding: chunked`` is rejected loudly (this reference
        client cannot dechunk). With a ``Content-Length`` the exact byte count
        is read; without one the body is connection-close framed — the adapter
        forced ``Connection: close``, so reading to EOF is correct.

        Args:
            reader: The connection's stream reader.
            headers: The parsed response headers.

        Returns:
            The raw response body bytes.

        Raises:
            ServiceResponseError: On a chunked response or a truncated body.
        """
        if _is_chunked(headers):
            raise ServiceResponseError(
                "Transfer-Encoding: chunked responses are not supported by this transport"
            )
        content_length = _content_length(headers)
        if content_length is None:
            # No Content-Length: connection-close framed. Read to EOF.
            return await asyncio.wait_for(reader.read(), timeout=self.timeout)
        return await asyncio.wait_for(
            reader.readexactly(content_length) if content_length > 0 else _empty(),
            timeout=self.timeout,
        )

    async def _read_headers(self, reader: asyncio.StreamReader) -> Headers:
        entries: list[tuple[str, str]] = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line or line in (b"\r\n", b"\n"):
                break
            text = line.decode("latin-1").rstrip("\r\n")
            if ":" not in text:
                raise ServiceResponseError(f"Malformed header: {text!r}")
            name, _, value = text.partition(":")
            entries.append((name.strip(), value.strip()))
        return Headers(entries)


def _split_url(url: Url) -> tuple[str, int, bool, str]:
    if not url.host:
        raise ServiceRequestError(f"URL missing host: {url!r}")
    secure = url.scheme == "https"
    port = url.port or (443 if secure else 80)
    path = url.path or "/"
    if len(url.query):
        path = f"{path}?{url.query.encode()}"
    return url.host, port, secure, path


def _host_header(host: str, port: int, secure: bool) -> str:
    """Build the ``Host`` header value, including the port when non-default.

    Args:
        host: The target host.
        port: The connection port.
        secure: Whether the connection is TLS (``https``).

    Returns:
        ``host`` for the scheme-default port, otherwise ``host:port``.
    """
    default = 443 if secure else 80
    return host if port == default else f"{host}:{port}"


def _parse_status(code: str) -> Status:
    """Build a `Status` from the status-line code, preserving in-range codes.

    ``Status`` synthesizes a member for any code in 100..599, so an
    unregistered-but-valid code (for example ``218`` or ``599``) yields a
    usable status carried through to the pipeline instead of being discarded.
    Only a genuinely out-of-range code raises.

    Args:
        code: The numeric status token from the status line.

    Returns:
        The resolved (possibly synthesized) status.

    Raises:
        ServiceResponseError: When ``code`` is outside the valid HTTP range.
    """
    try:
        return Status(int(code))
    except ValueError as err:
        raise ServiceResponseError(f"Invalid status code: {code}", error=err) from err


def _drain_body(body: RequestBody) -> bytes:
    """Materialise a request body into bytes.

    Runs on a worker thread (via ``asyncio.to_thread``) so the possibly-
    blocking sync iteration of file/stream-backed bodies does not stall the
    event loop.

    Args:
        body: The request body to read fully.

    Returns:
        The concatenated body bytes.
    """
    return b"".join(body.iter_bytes())


def _is_chunked(headers: Headers) -> bool:
    """Return whether the response declares chunked transfer-coding.

    Inspects every ``Transfer-Encoding`` line — the header may be split across
    multiple lines (e.g. ``gzip`` then ``chunked``), so reading only the first
    value would miss a chunked coding that is not listed first and then parse
    chunk-framing bytes as the body. Each line is a comma-separated coding
    list; the coding name (the token before any ``;`` parameters) is matched
    exactly, so a value that merely contains the substring ``chunked`` (e.g. an
    ``x-chunked`` coding name) does not trip the check. Per RFC 9112 §6.1 a
    response advertising chunked framing cannot be read as a fixed-length body.

    Args:
        headers: The parsed response headers.

    Returns:
        ``True`` if any ``Transfer-Encoding`` value names ``chunked``.
    """
    return any(
        coding.split(";")[0].strip().lower() == "chunked"
        for value in headers.values("Transfer-Encoding")
        for coding in value.split(",")
    )


def _content_length(headers: Headers) -> int | None:
    """Resolve the declared body length, or ``None`` when absent.

    Args:
        headers: The parsed response headers.

    Returns:
        The non-negative byte count, or ``None`` when no ``Content-Length``
        header is present (the caller then reads to EOF).

    Raises:
        ServiceResponseError: When the header is present but unparseable.
    """
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError as err:
        raise ServiceResponseError(f"Invalid Content-Length: {raw!r}", error=err) from err


async def _empty() -> bytes:
    return b""


__all__ = ["AsyncioHttpClient"]
