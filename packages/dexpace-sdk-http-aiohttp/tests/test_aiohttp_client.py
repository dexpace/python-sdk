# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AiohttpHttpClient`` against a local ``aiohttp.web`` server."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import aiohttp
import pytest
from aiohttp import web

from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.common.protocol import Protocol
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.request.request_body import RequestBody
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.http.aiohttp import AiohttpHttpClient
from dexpace.sdk.http.aiohttp.client import _frame_length, _wrap_response

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


async def _framing(request: web.Request) -> web.Response:
    """Reflect how the request was framed (length vs. chunked)."""
    body = await request.read()
    return web.Response(
        text="ok",
        headers={
            "X-Req-Content-Length": request.headers.get("Content-Length", ""),
            "X-Req-Transfer-Encoding": request.headers.get("Transfer-Encoding", ""),
            "X-Received-Bytes": str(len(body)),
        },
    )


async def _gzip(_request: web.Request) -> web.Response:
    """Return a gzip-compressed body so aiohttp transparently decodes it."""
    payload = b"decompressed-payload" * 64
    response = web.Response(body=payload)
    response.enable_compression(web.ContentCoding.gzip)
    return response


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
async def base_url() -> AsyncIterator[str]:
    """Start an aiohttp.web server on an ephemeral port; yield its base URL."""
    app = web.Application()
    app.router.add_get("/ok", _ok)
    app.router.add_route("POST", "/echo", _echo)
    app.router.add_get("/slow", _slow)
    app.router.add_get("/headers", _headers_echo)
    app.router.add_route("POST", "/framing", _framing)
    app.router.add_get("/gzip", _gzip)

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


async def test_content_length_extracted_from_response(base_url: str) -> None:
    """Aiohttp client should populate AsyncResponseBody.content_length from headers."""
    async with AiohttpHttpClient() as client:
        request = Request(method=Method.GET, url=Url.parse(f"{base_url}/ok"))
        async with await client.execute(request) as response:
            assert response.body is not None
            # /ok returns a small JSON; Content-Length should be set by aiohttp.
            assert response.body.content_length() > 0


# ----------------------------------------------------------------- unknown status


class _FakeAioResponse:
    """Minimal stand-in for an ``aiohttp.ClientResponse`` for ``_wrap_response``.

    Provides every attribute the non-discard path now reads: ``status``,
    ``headers`` (a ``CIMultiDict``), ``reason``, ``version``, and the
    body-reading surface ``content.read`` / ``release``.
    """

    def __init__(
        self,
        status: int,
        *,
        version: aiohttp.HttpVersion | None = aiohttp.HttpVersion(1, 1),
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        from multidict import CIMultiDict

        self.status = status
        self.headers = CIMultiDict(headers or {})
        self.reason = "Synthesized"
        self.version = version
        self.released = False
        self.content = _FakeContent(body)

    def release(self) -> None:  # aiohttp's release() is synchronous
        self.released = True


class _FakeContent:
    """Minimal ``aiohttp.StreamReader`` stand-in returning a fixed body."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._read = False

    async def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body


async def test_in_range_unregistered_status_yields_response() -> None:
    """An in-range, unregistered code builds a live Response — not an error."""
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    fake = _FakeAioResponse(
        599,  # in-range but not a named member
        headers={"Content-Length": "5"},
        body=b"hello",
    )

    response = _wrap_response(request, fake)  # type: ignore[arg-type]

    assert not fake.released, "live response must not be discarded"
    assert int(response.status) == 599
    assert response.body is not None
    assert await response.body.bytes() == b"hello"


async def test_apache_218_status_is_preserved() -> None:
    """Apache's 218 'This is fine' is in range and must round-trip as a Response."""
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    fake = _FakeAioResponse(218, headers={"Content-Length": "2"}, body=b"ok")

    response = _wrap_response(request, fake)  # type: ignore[arg-type]

    assert int(response.status) == 218
    assert response.status.is_success


def test_invalid_status_releases_connection_and_raises() -> None:
    """A genuinely invalid code (outside 100..599) releases then raises."""
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    fake = _FakeAioResponse(999)  # out of the valid HTTP range

    with pytest.raises(ServiceResponseError) as exc_info:
        _wrap_response(request, fake)  # type: ignore[arg-type]

    assert fake.released, "connection leaked: release() was not called"
    assert "999" in str(exc_info.value)


# ----------------------------------------------------------------- post-close


async def test_execute_after_aclose_raises() -> None:
    """A closed client must not resurrect; execute() raises ServiceRequestError."""
    client = AiohttpHttpClient(timeout=5.0)
    await client.aclose()
    with pytest.raises(ServiceRequestError, match="closed"):
        await client.execute(Request(method=Method.GET, url=Url.parse("http://example.test/")))


async def test_aclose_is_idempotent() -> None:
    """Calling aclose() twice is a no-op and stays in the closed state."""
    client = AiohttpHttpClient(timeout=5.0)
    await client.aclose()
    await client.aclose()  # must not raise
    with pytest.raises(ServiceRequestError, match="closed"):
        await client.execute(Request(method=Method.GET, url=Url.parse("http://example.test/")))


# ----------------------------------------------------------- connect timeout


class _ConnectTimeoutSession:
    """Stub session whose ``request`` raises aiohttp's connect-phase timeout.

    aiohttp raises ``ConnectionTimeoutError`` for a connect-scoped timeout
    (``connect=`` / ``sock_connect=``) and ``SocketTimeoutError`` for a read
    timeout (``sock_read=``); the client configures both so the two phases stay
    distinguishable. We drive the connect branch directly with the exception
    aiohttp raises so the test stays hermetic (no real unreachable-host connect).
    """

    def request(self, **_kwargs: object) -> _ConnectTimeoutSession:
        return self

    def __await__(self) -> object:
        raise aiohttp.ConnectionTimeoutError("connect timed out")
        yield  # pragma: no cover - makes this an awaitable generator


async def test_connect_timeout_maps_to_request_timeout() -> None:
    """A connect-phase timeout maps to ServiceRequestTimeoutError, not a response timeout."""
    client = AiohttpHttpClient(timeout=5.0, session=_ConnectTimeoutSession())  # type: ignore[arg-type]
    with pytest.raises(ServiceRequestTimeoutError):
        await client.execute(Request(method=Method.GET, url=Url.parse("http://example.test/")))


class _CaptureTimeoutSession:
    """Captures the ``ClientTimeout`` passed to ``request`` then fails the connect."""

    def __init__(self) -> None:
        self.captured: aiohttp.ClientTimeout | None = None

    def request(
        self, *, timeout: aiohttp.ClientTimeout, **_kwargs: object
    ) -> _CaptureTimeoutSession:
        self.captured = timeout
        return self

    def __await__(self) -> object:
        raise aiohttp.ConnectionTimeoutError("connect timed out")
        yield  # pragma: no cover - makes this an awaitable generator


async def test_timeout_configured_per_phase_so_connect_is_distinguishable() -> None:
    """The client asks aiohttp for per-phase sock_connect/sock_read, not a total budget.

    A total-only budget makes a connect-phase timeout raise a bare
    ``TimeoutError`` indistinguishable from a read timeout; per-phase config
    makes connect raise ``ConnectionTimeoutError`` so it maps to a request error.
    """
    session = _CaptureTimeoutSession()
    client = AiohttpHttpClient(timeout=5.0, session=session)  # type: ignore[arg-type]
    with pytest.raises(ServiceRequestTimeoutError):
        await client.execute(Request(method=Method.GET, url=Url.parse("http://example.test/")))
    cfg = session.captured
    assert cfg is not None
    assert cfg.sock_connect == 5.0
    assert cfg.sock_read == 5.0
    assert cfg.total is None


async def test_read_timeout_maps_to_response_timeout(base_url: str) -> None:
    """A read-phase (sock_read) timeout maps to ServiceResponseTimeoutError."""
    async with AiohttpHttpClient(timeout=0.25) as client:
        with pytest.raises(ServiceResponseTimeoutError):
            await client.execute(Request(method=Method.GET, url=Url.parse(f"{base_url}/slow")))


# --------------------------------------------------------------- request framing


async def test_known_length_body_is_length_framed_not_chunked(base_url: str) -> None:
    """A known-length body goes out with Content-Length, not Transfer-Encoding."""
    payload = b"hello world"
    async with AiohttpHttpClient(timeout=5.0) as client:
        response = await client.execute(
            Request(
                method=Method.POST,
                url=Url.parse(f"{base_url}/framing"),
                body=RequestBody.from_bytes(payload),
            )
        )
    assert response.status is Status.OK
    assert response.headers.get("x-req-content-length") == str(len(payload))
    # The decisive assertion: no chunked framing for a known-length body.
    assert (response.headers.get("x-req-transfer-encoding") or "") == ""
    assert response.headers.get("x-received-bytes") == str(len(payload))


async def test_unknown_length_body_still_chunks(base_url: str) -> None:
    """A single-use stream of unknown length falls back to chunked framing."""
    payload = b"streamed-bytes"
    body = RequestBody.from_iter([payload])  # content_length() == -1
    async with AiohttpHttpClient(timeout=5.0) as client:
        response = await client.execute(
            Request(
                method=Method.POST,
                url=Url.parse(f"{base_url}/framing"),
                body=body,
            )
        )
    assert response.status is Status.OK
    assert (response.headers.get("x-req-content-length") or "") == ""
    assert response.headers.get("x-req-transfer-encoding") == "chunked"
    assert response.headers.get("x-received-bytes") == str(len(payload))


def test_frame_length_sets_content_length_for_known_body() -> None:
    """_frame_length stamps Content-Length when the body length is known."""
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/"),
        body=RequestBody.from_bytes(b"abcd"),
    )
    headers = _frame_length(request)
    assert headers.get("content-length") == "4"


def test_frame_length_respects_existing_content_length() -> None:
    """_frame_length never overwrites a caller-supplied Content-Length."""
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/"),
        body=RequestBody.from_bytes(b"abcd"),
    ).with_header("Content-Length", "99")
    headers = _frame_length(request)
    assert headers.get("content-length") == "99"


def test_frame_length_leaves_unknown_length_unset() -> None:
    """_frame_length adds nothing for an unknown-length body."""
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/"),
        body=RequestBody.from_iter([b"x"]),
    )
    headers = _frame_length(request)
    assert headers.get("content-length") is None


# ----------------------------------------------------------- non-blocking upload


async def test_sync_body_iteration_runs_off_the_event_loop(base_url: str) -> None:
    """A blocking sync iterator must be pumped via a worker thread, not the loop.

    The body's ``iter_bytes`` blocks on a threading.Event for each chunk. If it
    were pumped inline on the loop, the heartbeat task below would be starved
    and the deadline would never release; pumping on a worker thread lets the
    loop keep running so the heartbeat can unblock the body.
    """
    import asyncio
    import threading

    gate = threading.Event()
    iterated_off_loop = False
    loop_thread_id = threading.get_ident()
    iter_thread_id: int | None = None

    class _BlockingBody(RequestBody):
        def media_type(self) -> None:
            return None

        def is_replayable(self) -> bool:
            return False

        def content_length(self) -> int:
            return -1

        def iter_bytes(self, chunk_size: int = 65536) -> Iterator[bytes]:
            nonlocal iterated_off_loop, iter_thread_id
            iter_thread_id = threading.get_ident()
            # Blocking here would freeze the loop if run inline.
            gate.wait(timeout=5.0)
            iterated_off_loop = True
            yield b"payload"

    async def _release_after_yield() -> None:
        # The loop must keep scheduling for this to run while the body blocks.
        await asyncio.sleep(0.05)
        gate.set()

    heartbeat = asyncio.create_task(_release_after_yield())
    async with AiohttpHttpClient(timeout=5.0) as client:
        response = await client.execute(
            Request(
                method=Method.POST,
                url=Url.parse(f"{base_url}/echo"),
                body=_BlockingBody(),
            )
        )
    await heartbeat
    assert iterated_off_loop
    # The decisive check: iteration ran on a worker thread, not the loop
    # thread. Inline (on-loop) pumping would record the loop's own id here and
    # this assertion would fail.
    assert iter_thread_id is not None
    assert iter_thread_id != loop_thread_id
    assert response.headers.get("x-received-bytes") == str(len(b"payload"))


# ------------------------------------------------------------ protocol reporting


async def test_protocol_reflects_http_version(base_url: str) -> None:
    """The reported protocol reflects the server's HTTP version (1.1 here)."""
    async with AiohttpHttpClient(timeout=5.0) as client:
        response = await client.execute(Request(method=Method.GET, url=Url.parse(f"{base_url}/ok")))
    assert response.protocol is Protocol.HTTP_1_1


def test_protocol_maps_http_10_and_defaults() -> None:
    """_protocol maps 1.0/2.x and falls back to 1.1 for unknown versions."""
    from dexpace.sdk.http.aiohttp.client import _protocol

    assert _protocol(_FakeAioResponse(200, version=aiohttp.HttpVersion(1, 0))) is (  # type: ignore[arg-type]
        Protocol.HTTP_1_0
    )
    assert _protocol(_FakeAioResponse(200, version=aiohttp.HttpVersion(2, 0))) is (  # type: ignore[arg-type]
        Protocol.HTTP_2
    )
    assert _protocol(_FakeAioResponse(200, version=None)) is Protocol.HTTP_1_1  # type: ignore[arg-type]


# ----------------------------------------------------------- content-encoding


async def test_content_encoding_drops_misleading_content_length(base_url: str) -> None:
    """A decoded (gzip) body must not advertise the compressed Content-Length."""
    async with AiohttpHttpClient(timeout=5.0) as client:
        response = await client.execute(
            Request(method=Method.GET, url=Url.parse(f"{base_url}/gzip"))
        )
        async with response:
            assert response.body is not None
            # aiohttp transparently decoded the body; the upstream length would
            # describe the compressed bytes, so it must be dropped.
            assert response.body.content_length() == -1
            payload = await response.body.bytes()
            assert payload == b"decompressed-payload" * 64
