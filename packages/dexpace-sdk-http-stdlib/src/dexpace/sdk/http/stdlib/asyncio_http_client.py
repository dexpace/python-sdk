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
- ``Content-Length``-framed responses (no chunked transfer-encoding)
- Connection: close (one request per connection)

Additional limitations that mirror `urllib_http_client`:

- **No streaming uploads.** The request body is fully buffered into memory
  via ``b"".join(request.body.iter_bytes())`` before send. For streaming
  uploads, plug in an alternative transport (``httpx``, ``aiohttp``).
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
from types import TracebackType
from typing import Final, Self

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

_DEFAULT_TIMEOUT: Final[float] = 30.0
_CRLF: Final[bytes] = b"\r\n"


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
            await self._send(writer, request, host, path)
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
            # response-side failure, not a connect failure.
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
        ssl_ctx = self._ssl_context or (_ssl.create_default_context() if secure else None)
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
        path: str,
    ) -> None:
        body_bytes = b""
        if request.body is not None:
            body_bytes = b"".join(request.body.iter_bytes())
        headers = Headers(request.headers.items())
        if "host" not in headers:
            # Preserve a caller-supplied Host (virtual hosting); only derive
            # it from the URL when the request did not set one.
            headers = headers.with_set("Host", host)
        if "content-length" not in headers and body_bytes:
            headers = headers.with_set("Content-Length", str(len(body_bytes)))
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
        try:
            status = Status(int(parts[1]))
        except ValueError as err:
            raise ServiceResponseError(f"Unknown status code: {parts[1]}", error=err) from err
        reason = parts[2] if len(parts) > 2 else None
        headers = await self._read_headers(reader)
        content_length = _content_length(headers)
        body_bytes = await asyncio.wait_for(
            reader.readexactly(content_length) if content_length > 0 else _empty(),
            timeout=self.timeout,
        )
        return AsyncResponse(
            request=request,
            protocol=Protocol.HTTP_1_1,
            status=status,
            headers=headers,
            reason=reason,
            body=AsyncResponseBody.from_bytes(body_bytes),
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


def _content_length(headers: Headers) -> int:
    raw = headers.get("Content-Length")
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except ValueError as err:
        raise ServiceResponseError(f"Invalid Content-Length: {raw!r}", error=err) from err


async def _empty() -> bytes:
    return b""


__all__ = ["AsyncioHttpClient"]
