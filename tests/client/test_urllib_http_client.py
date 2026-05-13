"""Tests for ``UrllibHttpClient`` against a tiny in-process TCP server."""

from __future__ import annotations

import socketserver
import threading
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.client import UrllibHttpClient
from dexpace.sdk.core.errors import ServiceRequestError
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import Status


def _build_response(method: str, body_len: int) -> bytes:
    payload = f'{{"method":"{method}","echo":{body_len}}}'.encode()
    headers = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
        b"X-Custom: yes\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    return headers + payload


class _EchoHandler(socketserver.StreamRequestHandler):
    """Minimal HTTP/1.1 echo handler — used in place of BaseHTTPRequestHandler.

    Reads the request line and headers, drains the body if any, and writes a
    fixed JSON response. Avoids ``BaseHTTPRequestHandler`` because its
    interaction with macOS network teardown can stall test shutdown.
    """

    def handle(self) -> None:
        request_line = self.rfile.readline().decode("latin-1", errors="replace")
        method = request_line.split(" ", 1)[0] if request_line else ""
        content_length = 0
        while True:
            line = self.rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            text = line.decode("latin-1", errors="replace").rstrip("\r\n")
            name, _, value = text.partition(":")
            if name.lower().strip() == "content-length":
                try:
                    content_length = int(value.strip())
                except ValueError:
                    content_length = 0
        body = self.rfile.read(content_length) if content_length else b""
        self.wfile.write(_build_response(method, len(body)))


class _TestServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def echo_server() -> Iterator[str]:
    """Start an in-process TCP server and yield its base URL."""
    server = _TestServer(("127.0.0.1", 0), _EchoHandler)
    host = "127.0.0.1"
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_get_round_trip(echo_server: str) -> None:
    client = UrllibHttpClient()
    request = Request(method=Method.GET, url=Url.parse(f"{echo_server}/"))
    with client:
        response = client.execute(request)
    assert response.status is Status.OK
    assert response.headers.get("x-custom") == "yes"
    body = response.body
    assert body is not None
    text = body.string()
    assert '"method":"GET"' in text


def test_post_with_body(echo_server: str) -> None:
    client = UrllibHttpClient()
    request = Request(
        method=Method.POST,
        url=Url.parse(f"{echo_server}/"),
        body=RequestBody.from_string("hello"),
    )
    with client:
        response = client.execute(request)
    body = response.body
    assert body is not None
    text = body.string()
    assert '"method":"POST"' in text
    assert '"echo":5' in text


def test_closed_client_raises() -> None:
    client = UrllibHttpClient()
    client.close()
    with pytest.raises(ServiceRequestError):
        client.execute(Request(method=Method.GET, url=Url.parse("http://127.0.0.1:1/")))


def test_invalid_timeout_raises() -> None:
    with pytest.raises(ValueError):
        UrllibHttpClient(timeout=0)


def test_connect_error_maps_to_service_request_error() -> None:
    client = UrllibHttpClient(timeout=1.0)
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1:1/"))
    with pytest.raises(ServiceRequestError):
        client.execute(request)
