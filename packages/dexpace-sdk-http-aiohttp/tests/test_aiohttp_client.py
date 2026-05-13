"""Tests for ``AiohttpHttpClient`` against a local ``aiohttp.web`` server."""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp import web

from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.http.aiohttp import AiohttpHttpClient

# ---------------------------------------------------------------------- handlers


async def _ok(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _echo(request: web.Request) -> web.Response:
    body = await request.read()
    return web.Response(
        body=body,
        headers={
            "Content-Type": request.headers.get("Content-Type", "application/octet-stream"),
            "X-Received-Bytes": str(len(body)),
        },
    )


async def _slow(_request: web.Request) -> web.Response:
    import asyncio as _asyncio

    await _asyncio.sleep(2.0)
    return web.Response(text="too late")


async def _headers_echo(request: web.Request) -> web.Response:
    return web.Response(
        text="ok",
        headers={
            "X-Echo-User-Agent": request.headers.get("User-Agent", ""),
            "X-Echo-Custom": request.headers.get("X-Custom", ""),
            "Set-Cookie": "a=1",
        },
    )


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
async def base_url() -> AsyncIterator[str]:
    """Start an aiohttp.web server on an ephemeral port; yield its base URL."""
    app = web.Application()
    app.router.add_get("/ok", _ok)
    app.router.add_route("POST", "/echo", _echo)
    app.router.add_get("/slow", _slow)
    app.router.add_get("/headers", _headers_echo)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets  # type: ignore[union-attr]
    assert sockets, "server has no listening sockets"
    port = int(sockets[0].getsockname()[1])
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------- tests


async def test_get_returns_200(base_url: str) -> None:
    async with AiohttpHttpClient(timeout=5.0) as client:
        response = await client.execute(Request(method=Method.GET, url=Url.parse(f"{base_url}/ok")))
    assert response.status is Status.OK
    content_type = response.headers.get("content-type") or ""
    assert content_type.startswith("application/json")
    body = response.body
    assert body is not None
    text = await body.string()
    assert '"ok"' in text


async def test_post_streams_body(base_url: str) -> None:
    payload = b"x" * (8192 * 3 + 17)  # spans multiple upload chunks
    async with AiohttpHttpClient(timeout=5.0) as client:
        response = await client.execute(
            Request(
                method=Method.POST,
                url=Url.parse(f"{base_url}/echo"),
                body=RequestBody.from_bytes(payload),
            )
        )
    assert response.status is Status.OK
    assert response.headers.get("x-received-bytes") == str(len(payload))
    body = response.body
    assert body is not None
    received = await body.bytes()
    assert received == payload


async def test_connect_error_maps_to_ServiceRequestError() -> None:  # noqa: N802
    """Port 1 on loopback refuses connections — should map to ServiceRequestError."""
    async with AiohttpHttpClient(timeout=2.0) as client:
        with pytest.raises(ServiceRequestError):
            await client.execute(Request(method=Method.GET, url=Url.parse("http://127.0.0.1:1/")))


async def test_timeout_maps_appropriately(base_url: str) -> None:
    async with AiohttpHttpClient(timeout=0.25) as client:
        with pytest.raises(ServiceResponseTimeoutError):
            await client.execute(Request(method=Method.GET, url=Url.parse(f"{base_url}/slow")))


async def test_headers_round_trip(base_url: str) -> None:
    request = (
        Request(
            method=Method.GET,
            url=Url.parse(f"{base_url}/headers"),
        )
        .with_header("User-Agent", "dexpace-test/1.0")
        .with_header("X-Custom", "hello")
    )
    async with AiohttpHttpClient(timeout=5.0) as client:
        response = await client.execute(request)
    assert response.status is Status.OK
    assert response.headers.get("x-echo-user-agent") == "dexpace-test/1.0"
    assert response.headers.get("x-echo-custom") == "hello"
    # Multi-value header preserved (aiohttp's CIMultiDict may collapse identical names —
    # at minimum the single value we sent must round-trip).
    assert response.headers.get("set-cookie") is not None


def test_invalid_timeout_raises() -> None:
    with pytest.raises(ValueError):
        AiohttpHttpClient(timeout=0)


async def test_shared_session_is_not_closed_on_aclose() -> None:
    session = aiohttp.ClientSession()
    try:
        client = AiohttpHttpClient(session=session)
        await client.aclose()
        assert not session.closed
    finally:
        await session.close()
