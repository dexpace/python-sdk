# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``UrllibHttpClient`` against a tiny in-process TCP server."""

from __future__ import annotations

import contextlib
import dataclasses
import http.client as http_client
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
from dexpace.sdk.core.http.common.headers import Headers
from dexpace.sdk.core.http.common.media_type import MediaType
from dexpace.sdk.core.http.common.protocol import Protocol
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


class _RedirectHandler(socketserver.StreamRequestHandler):
    """Replies with a 302 pointing at another origin and a small body.

    Pins that the transport must NOT follow the redirect itself. If it did,
    the second hop would fail (the target host is unroutable) or the response
    would carry the followed target's status — either way not a 302.
    """

    def handle(self) -> None:
        with contextlib.suppress(OSError):
            self.rfile.readline()
            while True:
                line = self.rfile.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
        body = b"moved"
        self.wfile.write(
            b"HTTP/1.1 302 Found\r\n"
            b"Location: http://10.255.255.1:81/elsewhere\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )


@pytest.fixture
def redirect_server() -> Iterator[str]:
    """Start a server that always replies 302; yield its base URL."""
    server = _TestServer(("127.0.0.1", 0), _RedirectHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_redirect_is_not_followed(redirect_server: str) -> None:
    # A 302 must surface to the pipeline as a 302 Response, not be
    # transparently followed by the transport (which would also leak the
    # Authorization header cross-origin).
    client = UrllibHttpClient(timeout=2.0)
    request = Request(method=Method.GET, url=Url.parse(f"{redirect_server}/"))
    with client:
        response = client.execute(request)
    assert response.status is Status.FOUND
    assert response.headers.get("location") == "http://10.255.255.1:81/elsewhere"
    body = response.body
    assert body is not None
    assert body.string() == "moved"


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


class _FakeHeaders:
    """Minimal ``email.message.Message``-like header container."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def items(self) -> list[tuple[str, str]]:
        return list(self._pairs)


class _TrackingResponse:
    """Stand-in for urllib's HTTPResponse exercised by ``_build_response``."""

    def __init__(
        self,
        status_code: int,
        *,
        headers: list[tuple[str, str]] | None = None,
        payload: bytes = b"",
        version: int | None = 11,
    ) -> None:
        self.status = status_code
        self.headers = _FakeHeaders(headers if headers is not None else [])
        self.reason = "Weird"
        self.version = version
        self.closed = False
        self._payload = payload

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size >= len(self._payload):
            out, self._payload = self._payload, b""
            return out
        out, self._payload = self._payload[:size], self._payload[size:]
        return out

    def close(self) -> None:
        self.closed = True


def test_in_range_unregistered_status_is_preserved_not_discarded() -> None:
    # IMP1: an in-range but unregistered code (599) must build a normal
    # Response carrying that status with a readable body — not be discarded.
    opened = _TrackingResponse(
        599,
        headers=[("Content-Length", "5")],
        payload=b"hello",
    )
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1/"))
    response = _urllib_mod._build_response(request, opened)
    assert int(response.status) == 599
    assert opened.closed is False
    body = response.body
    assert body is not None
    assert body.bytes() == b"hello"


@pytest.mark.req("TRANSPORT-24")
def test_out_of_range_status_surfaces_with_readable_body() -> None:
    # TRANSPORT-24: a genuinely out-of-range status code (outside 100..599)
    # no longer maps to an error — Status is total over integers, so the
    # response surfaces intact with its vendor code and body.
    opened = _TrackingResponse(999)
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1/"))
    response = _urllib_mod._build_response(request, opened)
    assert int(response.status) == 999
    assert opened.closed is False


def test_protocol_version_is_reported_from_http_response() -> None:
    # IMP7: report the actual protocol version where urllib exposes it.
    opened = _TrackingResponse(200, version=10)
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1/"))
    response = _urllib_mod._build_response(request, opened)
    assert response.protocol is Protocol.HTTP_1_0


def test_unknown_protocol_version_defaults_to_http_1_1() -> None:
    opened = _TrackingResponse(200, version=None)
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1/"))
    response = _urllib_mod._build_response(request, opened)
    assert response.protocol is Protocol.HTTP_1_1


def test_content_length_surfaced_under_content_encoding() -> None:
    # http.client does not decode Content-Encoding, so the body urllib serves
    # is the wire payload whose length is the upstream Content-Length: the
    # header is accurate and is surfaced as-is (unlike the decompressing
    # requests / httpx / aiohttp adapters, which must drop it).
    opened = _TrackingResponse(
        200,
        headers=[("Content-Length", "9"), ("Content-Encoding", "gzip")],
        payload=b"compressed",
    )
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1/"))
    response = _urllib_mod._build_response(request, opened)
    body = response.body
    assert body is not None
    assert body.content_length() == 9


def test_read_failure_maps_to_service_response_error() -> None:
    # A read-phase failure on the raw HTTPResponse must surface as an
    # SdkError, not a bare OSError / IncompleteRead.
    class _BoomResponse(_TrackingResponse):
        def read(self, size: int = -1) -> bytes:
            raise http_client.IncompleteRead(b"partial")

    opened = _BoomResponse(200, headers=[("Content-Length", "100")])
    request = Request(method=Method.GET, url=Url.parse("http://127.0.0.1/"))
    response = _urllib_mod._build_response(request, opened)
    body = response.body
    assert body is not None
    with pytest.raises(ServiceResponseError):
        body.bytes()


@dataclasses.dataclass
class _Captured:
    """What a `_CapturingHandler` actually received on the wire."""

    request_line: str = ""
    headers: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    body: bytes = b""


class _CapturingHandler(socketserver.StreamRequestHandler):
    """Records the request line, headers, and body it actually received."""

    captured: _Captured = _Captured()

    def handle(self) -> None:
        request_line = self.rfile.readline().decode("latin-1").rstrip("\r\n")
        headers: list[tuple[str, str]] = []
        content_length = 0
        while True:
            line = self.rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            text = line.decode("latin-1").rstrip("\r\n")
            name, _, value = text.partition(":")
            headers.append((name.strip(), value.strip()))
            if name.strip().lower() == "content-length":
                content_length = int(value.strip())
        body = self.rfile.read(content_length) if content_length else b""
        type(self).captured = _Captured(request_line=request_line, headers=headers, body=body)
        payload = b'{"ok":true}'
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )


@pytest.fixture
def capturing_server() -> Iterator[str]:
    """Start a server that records the request it receives; yield its base URL."""
    _CapturingHandler.captured = _Captured()
    server = _TestServer(("127.0.0.1", 0), _CapturingHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _sent_header(name: str) -> str | None:
    for key, value in _CapturingHandler.captured.headers:
        if key.lower() == name.lower():
            return value
    return None


def test_content_type_derived_from_body_media_type(capturing_server: str) -> None:
    # A body with a declared media type but no caller-set Content-Type must
    # have that media type surface on the wire, not urllib's own
    # application/x-www-form-urlencoded legacy default.
    body = RequestBody.from_bytes(b"{}", MediaType.of("application", "json"))
    request = Request(method=Method.POST, url=Url.parse(f"{capturing_server}/"), body=body)
    with UrllibHttpClient(timeout=2.0) as client:
        client.execute(request).close()
    assert _sent_header("Content-Type") == "application/json"


def test_explicit_content_type_is_authoritative(capturing_server: str) -> None:
    body = RequestBody.from_bytes(b"{}", MediaType.of("application", "json"))
    request = Request(
        method=Method.POST,
        url=Url.parse(f"{capturing_server}/"),
        headers=Headers([("Content-Type", "text/plain")]),
        body=body,
    )
    with UrllibHttpClient(timeout=2.0) as client:
        client.execute(request).close()
    assert _sent_header("Content-Type") == "text/plain"


def test_stale_content_length_header_is_recomputed(capturing_server: str) -> None:
    # A caller-supplied Content-Length that disagrees with the real body must
    # never reach the wire: trusting it would make the peer wait for bytes
    # that never arrive (previously this caused a hang / read timeout).
    request = Request(
        method=Method.POST,
        url=Url.parse(f"{capturing_server}/"),
        headers=Headers([("Content-Length", "999")]),
        body=RequestBody.from_bytes(b"hello world"),
    )
    with UrllibHttpClient(timeout=2.0) as client:
        response = client.execute(request)
        response.close()
    assert _sent_header("Content-Length") == "11"
    assert _CapturingHandler.captured.body == b"hello world"


def test_per_call_timeout_overrides_client_default() -> None:
    # A per-request timeout must reach urlopen, not just the client's own
    # constructor default. Proven by racing a *short* per-call timeout
    # against a blackhole address with a *long* constructor default: if the
    # override were ignored, this would hang for the full 30s default
    # instead of failing in well under it.
    start = time.monotonic()
    request = Request(
        method=Method.GET,
        url=Url.parse("http://10.255.255.1:81/"),
        timeout=0.3,
    )
    with UrllibHttpClient(timeout=30.0) as client, pytest.raises(ServiceRequestTimeoutError):
        client.execute(request)
    assert time.monotonic() - start < 5.0


class _GarbledHandler(socketserver.StreamRequestHandler):
    """Drains the request, then replies with a non-HTTP status line."""

    def handle(self) -> None:
        self.rfile.readline()
        while True:
            line = self.rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
        self.wfile.write(b"NOT A VALID STATUS LINE\r\n\r\n")


@pytest.fixture
def garbled_server() -> Iterator[str]:
    """Start a server that replies with a garbled, non-HTTP status line."""
    server = _TestServer(("127.0.0.1", 0), _GarbledHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


class _MalformedNameHandler(socketserver.StreamRequestHandler):
    """Replies with a header name `http.client` keeps but `Headers` rejects.

    A name containing a space (``Bad Name``) is dropped by ``http.client``'s
    own ``email``-based parser before this adapter ever sees it. To exercise
    the adapter's *own* per-entry lenient conversion, the name must be one
    ``http.client`` preserves (and keeps subsequent headers after) yet
    ``Headers`` rejects as a non-token — ``X@Bad`` fits (``@`` is a
    separator, not a ``tchar``). The adapter must drop just that entry and
    keep the good headers on both sides of it.
    """

    def handle(self) -> None:
        self.rfile.readline()
        while True:
            line = self.rfile.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
        body = b"hi"
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nX-Good: ok\r\nX@Bad: x\r\nX-After: yes\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )


def test_malformed_response_header_dropped_others_preserved() -> None:
    server = _TestServer(("127.0.0.1", 0), _MalformedNameHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = UrllibHttpClient(timeout=2.0)
        request = Request(method=Method.GET, url=Url.parse(f"http://127.0.0.1:{port}/"))
        with client, client.execute(request) as response:
            assert response.headers.get("x-good") == "ok"
            assert response.headers.get("x-after") == "yes"
            assert "x@bad" not in response.headers.names()
    finally:
        server.shutdown()
        server.server_close()


def test_malformed_response_maps_to_service_request_error(garbled_server: str) -> None:
    # A garbled/non-HTTP response is not wrapped in URLError by urllib —
    # http.client raises its own exception hierarchy (e.g. BadStatusLine)
    # directly out of getresponse(). Previously this escaped as a raw
    # http.client.HTTPException instead of an SdkError.
    client = UrllibHttpClient(timeout=2.0)
    request = Request(method=Method.GET, url=Url.parse(f"{garbled_server}/"))
    with pytest.raises(ServiceRequestError):
        client.execute(request)
