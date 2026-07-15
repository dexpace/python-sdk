# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``AsyncHttpxHttpClient`` using `httpx.MockTransport`."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from dexpace.sdk.core.errors import (
    SdkError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.http.httpx import AsyncHttpxHttpClient


def _ok_handler(payload: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-custom": "yes"},
            content=payload,
        )

    return handler


async def test_async_get_returns_200() -> None:
    payload = b'{"ok":true}'
    transport = httpx.MockTransport(_ok_handler(payload))
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/v1"))
        async with await client.execute(request) as response:
            assert response.status == Status(200)
            assert response.body is not None
            body_bytes = await response.body.bytes()
    assert body_bytes == payload


async def test_async_post_streams_body() -> None:
    received: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = request.content
        received["method"] = request.method.encode()
        return httpx.Response(200, content=b"ack")

    transport = httpx.MockTransport(handler)
    chunks = [b"chunk-one;", b"chunk-two;", b"chunk-three"]
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/upload"),
        body=RequestBody.from_iter(iter(chunks)),
    )
    async with AsyncHttpxHttpClient(transport=transport) as client:
        response = await client.execute(request)
        async with response:
            assert response.status == Status(200)

    assert received["body"] == b"chunk-one;chunk-two;chunk-three"
    assert received["method"] == b"POST"


async def test_async_connect_error_maps_to_service_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestError):
            await client.execute(request)


async def test_async_connect_timeout_maps_to_service_request_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout", request=request)

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestTimeoutError):
            await client.execute(request)


async def test_async_read_timeout_maps_to_service_response_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceResponseTimeoutError):
            await client.execute(request)


async def test_async_pool_timeout_maps_to_service_request_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool timeout", request=request)

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestTimeoutError):
            await client.execute(request)


async def test_async_write_timeout_maps_to_service_request_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("write timeout", request=request)

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestTimeoutError):
            await client.execute(request)


async def test_async_other_request_error_maps_to_service_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.NetworkError("network gremlin", request=request)

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestError):
            await client.execute(request)


async def test_async_headers_round_trip() -> None:
    sent: dict[str, list[tuple[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["headers"] = list(request.headers.items())
        return httpx.Response(
            200,
            headers=[
                ("Content-Type", "application/json"),
                ("X-Server", "mock-1"),
                ("X-Server", "mock-2"),
            ],
            content=b"{}",
        )

    transport = httpx.MockTransport(handler)
    from dexpace.sdk.core.http.common.headers import Headers as SdkHeaders

    headers = SdkHeaders([("Accept", "application/json"), ("X-Trace-Id", "abc123")])
    request = Request(
        method=Method.GET,
        url=Url.parse("http://example.test/headers"),
        headers=headers,
    )
    async with AsyncHttpxHttpClient(transport=transport) as client:
        response = await client.execute(request)
        async with response:
            assert response.status == Status(200)
            assert response.headers.get("content-type") == "application/json"
            assert response.headers.values("x-server") == ("mock-1", "mock-2")

    sent_dict = dict(sent["headers"])
    assert sent_dict.get("accept") == "application/json"
    assert sent_dict.get("x-trace-id") == "abc123"


async def test_async_execute_after_close_raises() -> None:
    transport = httpx.MockTransport(_ok_handler(b"{}"))
    client = AsyncHttpxHttpClient(transport=transport)
    await client.aclose()
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with pytest.raises(ServiceRequestError):
        await client.execute(request)


async def test_async_reason_phrase_propagated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        async with await client.execute(request) as response:
            assert response.reason == "OK"


async def test_async_known_length_body_sends_content_length_not_chunked() -> None:
    """A known-length body goes out framed by Content-Length, not chunked."""
    sent: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["content-length"] = request.headers.get("content-length")
        sent["transfer-encoding"] = request.headers.get("transfer-encoding")
        return httpx.Response(200, content=b"ack")

    transport = httpx.MockTransport(handler)
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/upload"),
        body=RequestBody.from_bytes(b"hello world"),
    )
    async with AsyncHttpxHttpClient(transport=transport) as client, await client.execute(request):
        pass

    assert sent["content-length"] == "11"
    assert sent["transfer-encoding"] is None


async def test_async_unknown_length_body_stays_chunked() -> None:
    """An unknown-length (iterator) body keeps Transfer-Encoding: chunked."""
    sent: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["content-length"] = request.headers.get("content-length")
        sent["transfer-encoding"] = request.headers.get("transfer-encoding")
        return httpx.Response(200, content=b"ack")

    transport = httpx.MockTransport(handler)
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/upload"),
        body=RequestBody.from_iter(iter([b"a", b"bc"])),
    )
    async with AsyncHttpxHttpClient(transport=transport) as client, await client.execute(request):
        pass

    assert sent["content-length"] is None
    assert sent["transfer-encoding"] == "chunked"


async def test_async_sync_body_iteration_runs_off_the_loop() -> None:
    """Blocking chunk reads run on a worker thread, not the event loop."""
    import threading

    loop_thread = threading.get_ident()
    iter_threads: list[int] = []

    def chunks() -> object:
        for piece in (b"one", b"two", b"three"):
            iter_threads.append(threading.get_ident())
            yield piece

    received: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = request.content
        return httpx.Response(200, content=b"ack")

    transport = httpx.MockTransport(handler)
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/upload"),
        body=RequestBody.from_iter(chunks()),  # type: ignore[arg-type]
    )
    async with AsyncHttpxHttpClient(transport=transport) as client, await client.execute(request):
        pass

    assert received["body"] == b"onetwothree"
    assert iter_threads, "iterator was never pumped"
    assert all(tid != loop_thread for tid in iter_threads), (
        "sync body iteration ran on the event loop thread"
    )


async def test_async_caller_content_length_is_not_duplicated() -> None:
    """An explicit caller Content-Length is preserved, not re-added."""
    from dexpace.sdk.core.http.common.headers import Headers as SdkHeaders

    sent: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["content-length"] = [
            v for k, v in request.headers.multi_items() if k == "content-length"
        ]
        return httpx.Response(200, content=b"ack")

    transport = httpx.MockTransport(handler)
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/upload"),
        headers=SdkHeaders([("Content-Length", "11")]),
        body=RequestBody.from_bytes(b"hello world"),
    )
    async with AsyncHttpxHttpClient(transport=transport) as client, await client.execute(request):
        pass

    assert sent["content-length"] == ["11"]


async def test_async_reported_protocol_reflects_http_version() -> None:
    """The response protocol mirrors httpx's reported HTTP version."""
    from dexpace.sdk.core.http.common.protocol import Protocol

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", extensions={"http_version": b"HTTP/2"})

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        async with await client.execute(request) as response:
            assert response.protocol == Protocol.HTTP_2


async def test_async_content_encoded_response_drops_content_length() -> None:
    """A content-encoded response does not propagate the upstream length."""
    import gzip

    compressed = gzip.compress(b"decompressed-bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-length": str(len(compressed))},
            content=compressed,
        )

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        async with await client.execute(request) as response:
            assert response.body is not None
            # The header length describes the compressed payload; the stream
            # yields decompressed bytes, so the length must not be propagated.
            assert response.body.content_length() == -1
            assert await response.body.bytes() == b"decompressed-bytes"


async def test_async_shared_client_is_not_closed_on_aclose() -> None:
    """A caller-supplied client is not closed by the transport."""
    transport = httpx.MockTransport(_ok_handler(b"{}"))
    shared = httpx.AsyncClient(transport=transport)
    client = AsyncHttpxHttpClient(client=shared)
    await client.aclose()
    assert not shared.is_closed
    await shared.aclose()


async def test_async_in_range_unregistered_status_returns_response() -> None:
    """An in-range but unregistered status yields a Response, not an error."""
    payload = b"upstream said 218"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(218, headers={"x-marker": "yes"}, content=payload)

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        async with await client.execute(request) as response:
            assert int(response.status) == 218
            assert response.headers.get("x-marker") == "yes"
            assert response.body is not None
            assert await response.body.bytes() == payload


@pytest.mark.req("TRANSPORT-24")
async def test_async_out_of_range_status_surfaces_with_readable_body() -> None:
    """TRANSPORT-24: an out-of-range status surfaces intact with its body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(999, content=b"hello")

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        response = await client.execute(request)
        assert int(response.status) == 999
        assert response.body is not None
        assert await response.body.bytes() == b"hello"


async def test_async_byo_client_second_close_is_a_noop() -> None:
    """Closing an adapter over a BYO client twice never touches the client."""
    transport = httpx.MockTransport(_ok_handler(b"{}"))
    shared = httpx.AsyncClient(transport=transport)
    client = AsyncHttpxHttpClient(client=shared)
    await client.aclose()
    await client.aclose()  # idempotent, non-blocking
    assert not shared.is_closed
    await shared.aclose()


async def test_async_byo_client_with_follow_redirects_true_is_overridden() -> None:
    """A BYO client's own ``follow_redirects=True`` never leaks through.

    ``follow_redirects=False`` is pinned explicitly on every ``.send()``
    call, not just relied on as the client's construction-time default, so a
    caller-supplied client built with ``follow_redirects=True`` still never
    has its 3xx responses auto-followed by httpx itself — that decision
    belongs solely to the SDK's own redirect policy.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://example.test/moved"})

    transport = httpx.MockTransport(handler)
    shared = httpx.AsyncClient(transport=transport, follow_redirects=True)
    async with AsyncHttpxHttpClient(client=shared) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        async with await client.execute(request) as response:
            assert response.status == Status(302)
            assert response.headers.get("location") == "http://example.test/moved"
    assert calls == 1
    await shared.aclose()


async def test_async_owned_client_disables_trust_env() -> None:
    """An adapter-constructed client never trusts the process environment.

    httpx's own ``trust_env`` implicitly reads ``HTTP_PROXY`` /
    ``HTTPS_PROXY`` / ``NO_PROXY``; proxy routing must only ever happen
    through the SDK's own opt-in ``ProxyOptions`` resolution, so a client
    this adapter builds itself pins ``trust_env=False``.
    """
    async with AsyncHttpxHttpClient() as client:
        assert client._client.trust_env is False


# --------------------------------------------------------------------------- #
# Exception-mapping matrix: proves the check ORDER, not just the final type.
#
# ``ConnectTimeout`` / ``ReadTimeout`` / ``WriteTimeout`` / ``PoolTimeout`` are
# all ``httpx.TimeoutException`` subtypes; ``ConnectError`` / ``ReadError`` are
# ``httpx.NetworkError`` subtypes; ``RemoteProtocolError`` is a
# ``httpx.ProtocolError``. All of these are also ``httpx.TransportError``. If
# the broad ``except httpx.RequestError`` clause in ``execute`` ran BEFORE the
# specific timeout clauses, every timeout below would misclassify as
# ``ServiceRequestError`` regardless of which phase actually timed out — this
# matrix would catch that regression.
# --------------------------------------------------------------------------- #

_ASYNC_EXCEPTION_MATRIX: tuple[tuple[type[httpx.RequestError], type[SdkError]], ...] = (
    (httpx.ConnectTimeout, ServiceRequestTimeoutError),
    (httpx.ReadTimeout, ServiceResponseTimeoutError),
    (httpx.WriteTimeout, ServiceRequestTimeoutError),
    (httpx.PoolTimeout, ServiceRequestTimeoutError),
    (httpx.ConnectError, ServiceRequestError),
    (httpx.ReadError, ServiceRequestError),
    (httpx.RemoteProtocolError, ServiceRequestError),
)


@pytest.mark.parametrize(("native_exc", "expected"), _ASYNC_EXCEPTION_MATRIX)
async def test_async_exception_mapping_matrix(
    native_exc: type[httpx.RequestError], expected: type[SdkError]
) -> None:
    """Every native transport error maps to its documented SDK type, in order."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise native_exc("boom", request=request)

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(expected):
            await client.execute(request)
