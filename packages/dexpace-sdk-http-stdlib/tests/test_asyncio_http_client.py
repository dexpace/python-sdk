# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncioHttpClient`` against an in-loop asyncio server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.http.stdlib import AsyncioHttpClient

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
