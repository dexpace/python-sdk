"""Tests for ``AsyncioHttpClient`` against an in-loop asyncio server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from dexpace.sdk.core.client import AsyncioHttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Status


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
