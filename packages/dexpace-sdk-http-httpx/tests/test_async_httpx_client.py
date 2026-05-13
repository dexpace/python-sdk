"""Tests for ``AsyncHttpxHttpClient`` using :class:`httpx.MockTransport`."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from dexpace.sdk.core.errors import (
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


async def test_async_unknown_status_closes_response() -> None:
    """When Status() rejects an unknown code, the response must be released."""
    from dexpace.sdk.core.errors import ServiceResponseError

    closed = {"yes": False}

    class _TrackedResponse(httpx.Response):
        async def aclose(self) -> None:
            closed["yes"] = True
            await super().aclose()

    def handler(request: httpx.Request) -> httpx.Response:
        return _TrackedResponse(999, content=b"")

    transport = httpx.MockTransport(handler)
    async with AsyncHttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceResponseError):
            await client.execute(request)
    assert closed["yes"], "Response should be closed when status mapping fails"
