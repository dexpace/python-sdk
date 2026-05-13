"""Tests for ``HttpxHttpClient`` using :class:`httpx.MockTransport`."""

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
from dexpace.sdk.http.httpx import HttpxHttpClient


def _ok_handler(
    payload: bytes, *, content_type: str = "application/json"
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": content_type, "x-custom": "yes"},
            content=payload,
        )

    return handler


def test_get_returns_200() -> None:
    payload = b'{"ok":true}'
    transport = httpx.MockTransport(_ok_handler(payload))
    with HttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/v1"))
        with client.execute(request) as response:
            assert response.status == Status(200)
            assert response.body is not None
            body_bytes = response.body.bytes()
    assert body_bytes == payload


def test_post_streams_body() -> None:
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
    with HttpxHttpClient(transport=transport) as client, client.execute(request) as response:
        assert response.status == Status(200)

    assert received["body"] == b"chunk-one;chunk-two;chunk-three"
    assert received["method"] == b"POST"


def test_connect_error_maps_to_service_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    with HttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestError):
            client.execute(request)


def test_connect_timeout_maps_to_service_request_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout", request=request)

    transport = httpx.MockTransport(handler)
    with HttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestTimeoutError):
            client.execute(request)


def test_read_timeout_maps_to_service_response_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    transport = httpx.MockTransport(handler)
    with HttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceResponseTimeoutError):
            client.execute(request)


def test_pool_timeout_maps_to_service_request_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool timeout", request=request)

    transport = httpx.MockTransport(handler)
    with HttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestTimeoutError):
            client.execute(request)


def test_write_timeout_maps_to_service_request_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("write timeout", request=request)

    transport = httpx.MockTransport(handler)
    with HttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestTimeoutError):
            client.execute(request)


def test_other_request_error_maps_to_service_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.NetworkError("network gremlin", request=request)

    transport = httpx.MockTransport(handler)
    with HttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with pytest.raises(ServiceRequestError):
            client.execute(request)


def test_headers_round_trip() -> None:
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
    with HttpxHttpClient(transport=transport) as client, client.execute(request) as response:
        assert response.status == Status(200)
        assert response.headers.get("content-type") == "application/json"
        assert response.headers.values("x-server") == ("mock-1", "mock-2")

    sent_dict = dict(sent["headers"])
    assert sent_dict.get("accept") == "application/json"
    assert sent_dict.get("x-trace-id") == "abc123"


def test_execute_after_close_raises() -> None:
    transport = httpx.MockTransport(_ok_handler(b"{}"))
    client = HttpxHttpClient(transport=transport)
    client.close()
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with pytest.raises(ServiceRequestError):
        client.execute(request)


def test_reason_phrase_propagated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    transport = httpx.MockTransport(handler)
    with HttpxHttpClient(transport=transport) as client:
        request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
        with client.execute(request) as response:
            assert response.reason == "OK"
