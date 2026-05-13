"""Tests for ``RequestsHttpClient`` against a tiny in-process TCP server."""

from __future__ import annotations

import socketserver
import threading
import time
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.http.requests import RequestsHttpClient


def _build_response(method: str, body_len: int, echo_header: str | None) -> bytes:
    echo = echo_header if echo_header is not None else ""
    payload = (f'{{"method":"{method}","echo":{body_len},"x_echo":"{echo}"}}').encode()
    headers = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
        b"X-Custom: yes\r\n"
        b"X-Repeat: a, b\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    return headers + payload


class _EchoHandler(socketserver.StreamRequestHandler):
    """Minimal HTTP/1.1 echo handler that reflects method, body length, and X-Echo."""

    def handle(self) -> None:
        request_line = self.rfile.readline().decode("latin-1", errors="replace")
        method = request_line.split(" ", 1)[0] if request_line else ""
        content_length = 0
        echo: str | None = None
        while True:
            line = self.rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            text = line.decode("latin-1", errors="replace").rstrip("\r\n")
            name, _, value = text.partition(":")
            lname = name.lower().strip()
            if lname == "content-length":
                try:
                    content_length = int(value.strip())
                except ValueError:
                    content_length = 0
            elif lname == "x-echo":
                echo = value.strip()
        body = self.rfile.read(content_length) if content_length else b""
        self.wfile.write(_build_response(method, len(body), echo))


class _SlowHandler(socketserver.StreamRequestHandler):
    """Handler that accepts the connection but never sends a response."""

    def handle(self) -> None:
        # Drain headers then sleep — provokes a ReadTimeout on the client.
        while True:
            line = self.rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
        time.sleep(2.0)


class _TestServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def echo_server() -> Iterator[str]:
    """Start an in-process TCP echo server and yield its base URL."""
    server = _TestServer(("127.0.0.1", 0), _EchoHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def slow_server() -> Iterator[str]:
    """Start an in-process TCP server that never replies."""
    server = _TestServer(("127.0.0.1", 0), _SlowHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_get_returns_200(echo_server: str) -> None:
    client = RequestsHttpClient()
    request = Request(method=Method.GET, url=Url.parse(f"{echo_server}/"))
    with client:
        response = client.execute(request)
        assert response.status is Status.OK
        assert response.reason == "OK"
        body = response.body
        assert body is not None
        text = body.string()
    assert '"method":"GET"' in text


def test_post_streams_body(echo_server: str) -> None:
    client = RequestsHttpClient()
    payload = b"hello world"

    def chunks() -> Iterator[bytes]:
        yield payload[:5]
        yield payload[5:]

    request = Request(
        method=Method.POST,
        url=Url.parse(f"{echo_server}/"),
        body=RequestBody.from_iter(chunks(), content_length=len(payload)),
    )
    with client:
        response = client.execute(request)
        body = response.body
        assert body is not None
        # Consume via iter_bytes to exercise the streaming path.
        collected = b"".join(body.iter_bytes(4))
    text = collected.decode("utf-8")
    assert '"method":"POST"' in text
    assert f'"echo":{len(payload)}' in text


def test_headers_round_trip(echo_server: str) -> None:
    client = RequestsHttpClient()
    request = Request(
        method=Method.GET,
        url=Url.parse(f"{echo_server}/"),
    ).with_header("X-Echo", "ping")
    with client:
        response = client.execute(request)
        body = response.body
        assert body is not None
        text = body.string()
    assert '"x_echo":"ping"' in text
    assert response.headers.get("x-custom") == "yes"
    # Server emits ``X-Repeat: a, b`` — comma-joined value preserved verbatim.
    assert response.headers.get("x-repeat") == "a, b"


def test_connect_error_maps_to_service_request_error() -> None:
    # Port 1 on loopback refuses connections — yields a ConnectionError.
    client = RequestsHttpClient(timeout=2.0)
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1:1/"))
    with client, pytest.raises(ServiceRequestError):
        client.execute(request)


def test_read_timeout_maps_to_service_response_timeout_error(slow_server: str) -> None:
    client = RequestsHttpClient(timeout=0.5)
    request = Request(method=Method.GET, url=Url.parse(f"{slow_server}/"))
    with client, pytest.raises(ServiceResponseTimeoutError):
        client.execute(request)


def test_closed_client_raises() -> None:
    client = RequestsHttpClient()
    client.close()
    with pytest.raises(ServiceRequestError):
        client.execute(Request(method=Method.GET, url=Url.parse("http://127.0.0.1:1/")))


def test_invalid_timeout_raises() -> None:
    with pytest.raises(ValueError):
        RequestsHttpClient(timeout=0)
