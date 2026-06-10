# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncioHttpClient`` against an in-loop asyncio server."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.protocol import Protocol
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.http.stdlib import AsyncioHttpClient
from dexpace.sdk.http.stdlib import asyncio_http_client as _asyncio_mod

_Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _handle_ok(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Minimal HTTP/1.1 server: reads until headers end, writes a fixed response."""
    while True:
        line = await reader.readline()
        if not line or line == b"\r\n":
            break
    body = b'{"ok":true}'
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )
    await writer.drain()
    writer.close()


@pytest.fixture
async def server() -> AsyncIterator[str]:
    """Start an asyncio HTTP server on a random port; yield its base URL."""
    srv = await asyncio.start_server(_handle_ok, "127.0.0.1", 0)
    socks = srv.sockets
    assert socks, "server has no listening sockets"
    port = int(socks[0].getsockname()[1])
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.close()
        await srv.wait_closed()


async def test_get_round_trip(server: str) -> None:
    async with AsyncioHttpClient(timeout=5.0) as client:
        response = await client.execute(Request(method=Method.GET, url=Url.parse(f"{server}/")))
    assert response.status is Status.OK
    assert response.headers.get("content-type") == "application/json"
    body = response.body
    assert body is not None
    text = await body.string()
    assert text == '{"ok":true}'


async def test_connect_failure() -> None:
    client = AsyncioHttpClient(timeout=1.0)
    with pytest.raises(ServiceRequestError):
        await client.execute(Request(method=Method.GET, url=Url.parse("http://127.0.0.1:1/")))


def test_invalid_timeout_raises() -> None:
    with pytest.raises(ValueError):
        AsyncioHttpClient(timeout=0)


async def _serve(handler: _Handler) -> AsyncIterator[str]:
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    socks = srv.sockets
    assert socks, "server has no listening sockets"
    port = int(socks[0].getsockname()[1])
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.close()
        await srv.wait_closed()


async def _read_request_head(reader: asyncio.StreamReader) -> list[str]:
    """Drain the request line and headers, returning the decoded lines."""
    lines: list[str] = []
    while True:
        line = await reader.readline()
        if not line or line == b"\r\n":
            break
        lines.append(line.decode("latin-1").rstrip("\r\n"))
    return lines


async def test_caller_host_header_is_preserved() -> None:
    # A caller-supplied Host must survive (virtual hosting); the transport
    # must not overwrite it with the bare URL host.
    seen: dict[str, str] = {}

    async def echo_host(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for header in await _read_request_head(reader):
            name, _, value = header.partition(":")
            if name.strip().lower() == "host":
                seen["host"] = value.strip()
        body = b'{"ok":true}'
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()

    gen = _serve(echo_host)
    base = await anext(gen)
    try:
        request = Request(method=Method.GET, url=Url.parse(f"{base}/"))
        request = request.with_header("Host", "virtual.example.com")
        async with AsyncioHttpClient(timeout=5.0) as client:
            await client.execute(request)
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
    assert seen.get("host") == "virtual.example.com"


async def test_partial_body_maps_to_service_response_error() -> None:
    # Server promises more bytes than it sends, then drops the connection.
    async def short_body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nonly-a-few-bytes"
        )
        await writer.drain()
        writer.close()

    gen = _serve(short_body)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            with pytest.raises(ServiceResponseError):
                await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


async def test_oversized_header_maps_to_service_response_error() -> None:
    # A header line longer than the reader limit makes readline raise a bare
    # ValueError; it must surface as a ServiceResponseError, not leak.
    async def oversized(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        giant = b"X-Big: " + b"a" * (128 * 1024)
        writer.write(b"HTTP/1.1 200 OK\r\n" + giant + b"\r\n\r\n")
        await writer.drain()
        writer.close()

    gen = _serve(oversized)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            with pytest.raises(ServiceResponseError):
                await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


async def test_read_timeout_maps_to_service_response_timeout() -> None:
    # Server accepts and reads the request, then stalls before replying.
    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        await asyncio.sleep(5.0)
        writer.close()

    gen = _serve(stall)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=0.3) as client:
            with pytest.raises(ServiceResponseTimeoutError):
                await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


def _ok_response() -> bytes:
    body = b'{"ok":true}'
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
    )


async def _collect_head(handler_sink: dict[str, list[str]]) -> _Handler:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler_sink["head"] = await _read_request_head(reader)
        writer.write(_ok_response())
        await writer.drain()
        writer.close()

    return handler


def _header_value(head: list[str], name: str) -> str | None:
    for line in head:
        key, _, value = line.partition(":")
        if key.strip().lower() == name.lower():
            return value.strip()
    return None


async def test_host_header_includes_non_default_port() -> None:
    # A non-default port must appear in the Host header (RFC 9112 §3.2).
    sink: dict[str, list[str]] = {}
    gen = _serve(await _collect_head(sink))
    base = await anext(gen)
    port = base.rsplit(":", 1)[1]
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
    assert _header_value(sink["head"], "host") == f"127.0.0.1:{port}"


async def test_empty_post_body_sends_content_length_zero() -> None:
    # A body-bearing method with no payload must advertise
    # Content-Length: 0 (RFC 9110 §8.6).
    sink: dict[str, list[str]] = {}
    gen = _serve(await _collect_head(sink))
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            await client.execute(Request(method=Method.POST, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
    assert _header_value(sink["head"], "content-length") == "0"


async def test_empty_get_omits_content_length() -> None:
    # A GET with no body must not gain a spurious Content-Length: 0.
    sink: dict[str, list[str]] = {}
    gen = _serve(await _collect_head(sink))
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
    assert _header_value(sink["head"], "content-length") is None


async def test_post_with_body_sets_content_length() -> None:
    sink: dict[str, list[str]] = {}
    gen = _serve(await _collect_head(sink))
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            await client.execute(
                Request(
                    method=Method.POST,
                    url=Url.parse(f"{base}/"),
                    body=RequestBody.from_string("hello"),
                )
            )
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
    assert _header_value(sink["head"], "content-length") == "5"


async def test_plain_http_ignores_supplied_ssl_context() -> None:
    # A caller-supplied ssl_context must not trigger a TLS handshake on
    # a plain http:// URL — the request must still succeed over plaintext.
    async def ok(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        writer.write(_ok_response())
        await writer.drain()
        writer.close()

    gen = _serve(ok)
    base = await anext(gen)
    ctx = ssl.create_default_context()
    try:
        async with AsyncioHttpClient(timeout=5.0, ssl_context=ctx) as client:
            response = await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
            assert response.status is Status.OK
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


async def test_chunked_response_raises_service_response_error() -> None:
    # A chunked response cannot be dechunked by this reference client, so
    # it must fail loudly rather than silently return an empty body.
    async def chunked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        writer.write(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    gen = _serve(chunked)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            with pytest.raises(ServiceResponseError):
                await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


async def test_multiline_transfer_encoding_with_chunked_not_first_raises() -> None:
    # Transfer-Encoding may be split across lines with ``chunked`` NOT
    # first (alongside a misleading Content-Length). The client must still
    # detect chunked framing and refuse to read the bytes as a fixed-length
    # body, rather than parsing chunk framing as the payload — the exact
    # message-framing ambiguity that enables request smuggling.
    async def multiline_te(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: gzip\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Length: 5\r\n"
            b"Connection: close\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    gen = _serve(multiline_te)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            with pytest.raises(ServiceResponseError):
                await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


def test_is_chunked_matches_coding_token_not_substring() -> None:
    # The chunked check matches the coding token exactly: a real ``chunked``
    # coding (alone, after other codings, or with parameters) trips it, but a
    # coding name that merely contains the substring ``chunked`` (``x-chunked``)
    # does not — so a benign coding is never mistaken for chunked framing.
    assert _asyncio_mod._is_chunked(Headers([("Transfer-Encoding", "chunked")]))
    assert _asyncio_mod._is_chunked(Headers([("Transfer-Encoding", "gzip, chunked")]))
    assert _asyncio_mod._is_chunked(
        Headers([("Transfer-Encoding", "gzip"), ("Transfer-Encoding", "chunked")])
    )
    assert _asyncio_mod._is_chunked(Headers([("Transfer-Encoding", "chunked ; foo=bar")]))
    assert not _asyncio_mod._is_chunked(Headers([("Transfer-Encoding", "x-chunked")]))
    assert not _asyncio_mod._is_chunked(Headers([("Transfer-Encoding", "gzip")]))


async def test_connection_close_framed_body_read_to_eof() -> None:
    # A response without Content-Length is connection-close framed; the
    # body must be read to EOF, not fabricated as empty.
    async def close_framed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nstreamed-to-eof")
        await writer.drain()
        writer.close()

    gen = _serve(close_framed)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            response = await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
            body = response.body
            assert body is not None
            assert await body.string() == "streamed-to-eof"
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


async def test_in_range_unregistered_status_is_preserved() -> None:
    # IMP1: an in-range but unregistered status (218) must be carried through
    # to the pipeline with a readable body, not discarded.
    async def odd_status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        body = b"this is fine"
        writer.write(
            b"HTTP/1.1 218 This Is Fine\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    gen = _serve(odd_status)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            response = await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
            assert int(response.status) == 218
            body = response.body
            assert body is not None
            assert await body.string() == "this is fine"
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


async def test_invalid_status_raises_service_response_error() -> None:
    # A genuinely out-of-range status (999) is invalid HTTP and must raise.
    async def bad_status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        writer.write(b"HTTP/1.1 999 Nope\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    gen = _serve(bad_status)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            with pytest.raises(ServiceResponseError):
                await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)


async def test_protocol_version_reported_from_status_line() -> None:
    # IMP7: report the actual HTTP version from the status line.
    async def http10(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request_head(reader)
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    gen = _serve(http10)
    base = await anext(gen)
    try:
        async with AsyncioHttpClient(timeout=5.0) as client:
            response = await client.execute(Request(method=Method.GET, url=Url.parse(f"{base}/")))
            assert response.protocol is Protocol.HTTP_1_0
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
