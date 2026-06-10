# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``UrllibHttpClient`` against a tiny in-process TCP server."""

from __future__ import annotations

import contextlib
import socketserver
import threading
import time
from collections.abc import Iterator

import pytest

from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Url
from dexpace.sdk.core.http.request import Method, Request, RequestBody
from dexpace.sdk.core.http.response import Status
from dexpace.sdk.http.stdlib import UrllibHttpClient
from dexpace.sdk.http.stdlib import urllib_http_client as _urllib_mod


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


class _StallHandler(socketserver.BaseRequestHandler):
    """Accepts the connection, reads the request, then never replies.

    Models a read-phase timeout: the TCP connection is established (so the
    request is fully transmitted) but the server stalls before sending a
    status line, so ``urlopen`` times out during the read phase.
    """

    def handle(self) -> None:
        with contextlib.suppress(OSError):
            self.request.recv(65536)
        time.sleep(5.0)


@pytest.fixture
def stall_server() -> Iterator[str]:
    """Start a server that accepts then stalls; yield its base URL."""
    server = _TestServer(("127.0.0.1", 0), _StallHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_read_timeout_maps_to_service_response_timeout(stall_server: str) -> None:
    # Accept-then-stall: the request was sent but the response never arrives.
    # Since 3.10 this surfaces as a bare TimeoutError, which must classify as
    # a *response* timeout (not request) so non-idempotent reads are not
    # silently retried.
    client = UrllibHttpClient(timeout=0.5)
    request = Request(method=Method.GET, url=Url.parse(f"{stall_server}/"))
    with pytest.raises(ServiceResponseTimeoutError):
        client.execute(request)


def test_connect_timeout_maps_to_service_request_timeout() -> None:
    # Blackhole connect: 10.255.255.1 is non-routable, so the connection is
    # never established. urllib surfaces this as URLError(reason=TimeoutError),
    # which must classify as a *request* timeout (retry-safe).
    client = UrllibHttpClient(timeout=0.5)
    request = Request(method=Method.GET, url=Url.parse("http://10.255.255.1:81/"))
    with pytest.raises(ServiceRequestTimeoutError):
        client.execute(request)


class _TrackingResponse:
    """Stand-in for urllib's HTTPResponse that records whether it was closed."""

    def __init__(self, status_code: int) -> None:
        self.status = status_code
        self.headers = None
        self.reason = "Weird"
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_unregistered_status_closes_response_before_raising() -> None:
    # A valid-but-unregistered status code makes Status(code) raise. The
    # underlying response must be released first so the connection is not
    # leaked.
    opened = _TrackingResponse(599)
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1/"))
    with pytest.raises(ServiceResponseError):
        _urllib_mod._build_response(request, opened)
    assert opened.closed is True
