# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Session/connector ownership matrix and cancellation propagation.

``test_aiohttp_client.py`` already covers ``aclose()`` idempotency and that a
caller-supplied session survives close. This file goes one level deeper on
two facets PY-M8-04 calls out specifically:

- The BYO case must leave the *connector* untouched too, not just the
  session (a session can report ``closed is False`` while its connector was
  independently torn down).
- The owned case must release the underlying connector *exactly once*, even
  under a redundant `aclose()` call — proven by counting the real close
  invocation rather than only inspecting the final ``closed`` state, which
  cannot distinguish "closed once" from "closed twice".

It also adds a dedicated test proving ``asyncio.CancelledError`` propagates
natively through `AiohttpHttpClient.execute` — never swallowed or converted
into an SDK error — which the shared conformance battery does not exercise
(its own cancellation test targets the response body iterator, not `execute`
itself).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.http.aiohttp import AiohttpHttpClient


async def _ok(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _slow(_request: web.Request) -> web.Response:
    await asyncio.sleep(2.0)
    return web.Response(text="too late")


@pytest.fixture
async def base_url() -> AsyncIterator[str]:
    """Start an aiohttp.web server on an ephemeral port; yield its base URL."""
    app = web.Application()
    app.router.add_get("/ok", _ok)
    app.router.add_get("/slow", _slow)
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


# --------------------------------------------------------------------------- #
# Session/connector ownership matrix.
# --------------------------------------------------------------------------- #


@pytest.mark.req("ASYNC-15", "SEAM-14", "SEAM-25", "TRANSPORT-15")
async def test_byo_session_and_connector_survive_close(base_url: str) -> None:
    """A caller-supplied session AND its connector survive `aclose()`."""
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector)
    try:
        client = AiohttpHttpClient(session=session)
        request = Request(method=Method.GET, url=Url.parse(f"{base_url}/ok"))
        async with await client.execute(request) as response:
            assert response.body is not None
            await response.body.bytes()

        await client.aclose()

        assert session.closed is False
        assert connector.closed is False
    finally:
        await session.close()


@pytest.mark.req("ASYNC-15", "ASYNC-16", "SEAM-14", "SEAM-25", "TRANSPORT-16")
async def test_owned_session_and_connector_released_exactly_once(base_url: str) -> None:
    """An adapter-owned connector is closed exactly once.

    Even across a redundant `aclose()` call.
    """
    client = AiohttpHttpClient(timeout=5.0)
    request = Request(method=Method.GET, url=Url.parse(f"{base_url}/ok"))
    async with await client.execute(request) as response:
        assert response.body is not None
        await response.body.bytes()

    session = client._session
    assert session is not None
    connector = session.connector
    assert connector is not None
    real_close = connector.close
    calls = 0

    async def _counting_close(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        await real_close(*args, **kwargs)

    connector.close = _counting_close  # type: ignore[method-assign]

    await client.aclose()
    await client.aclose()  # idempotent: must not release a second time

    assert calls == 1
    assert connector.closed is True


# --------------------------------------------------------------------------- #
# Cancellation propagation.
# --------------------------------------------------------------------------- #


@pytest.mark.req("ASYNC-6", "SEAM-13", "TRANSPORT-7")
async def test_cancelled_error_propagates_natively_through_execute(base_url: str) -> None:
    """``asyncio.CancelledError`` is never swallowed or remapped by `execute`.

    None of `execute`'s ``except`` clauses catch `BaseException` broadly, so
    cancelling the coroutine mid-flight should surface the *native*
    ``asyncio.CancelledError`` unchanged — not a `ServiceRequestError` /
    `ServiceResponseTimeoutError` / any other SDK-mapped type.
    """
    async with AiohttpHttpClient(timeout=30.0) as client:
        request = Request(method=Method.GET, url=Url.parse(f"{base_url}/slow"))
        task = asyncio.create_task(client.execute(request))
        await asyncio.sleep(0.05)  # let the request reach the in-flight read
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
