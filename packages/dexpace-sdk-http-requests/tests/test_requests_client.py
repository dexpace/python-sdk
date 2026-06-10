# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``RequestsHttpClient`` against a tiny in-process TCP server."""

from __future__ import annotations

import io
import pathlib
import socketserver
import threading
import time
from collections.abc import Iterator

import pytest
import requests
import requests.structures

from dexpace.sdk.core.errors import (
    ServiceRequestError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from dexpace.sdk.core.http.common import Headers, Url
from dexpace.sdk.core.http.common.protocol import Protocol
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
    """Minimal HTTP/1.1 echo handler reflecting method, body length, and X-Echo.

    Reads the body framed either by ``Content-Length`` or by
    ``Transfer-Encoding: chunked``, so requests using one framing scheme are
    not silently miscounted as zero-length under the other.
    """

    def handle(self) -> None:
        request_line = self.rfile.readline().decode("latin-1", errors="replace")
        method = request_line.split(" ", 1)[0] if request_line else ""
        content_length = 0
        chunked = False
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
            elif lname == "transfer-encoding" and "chunked" in value.lower():
                chunked = True
            elif lname == "x-echo":
                echo = value.strip()
        body = self._read_chunked() if chunked else self._read_sized(content_length)
        self.wfile.write(_build_response(method, len(body), echo))

    def _read_sized(self, length: int) -> bytes:
        return self.rfile.read(length) if length else b""

    def _read_chunked(self) -> bytes:
        body = bytearray()
        while True:
            size_line = self.rfile.readline()
            size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                self.rfile.readline()  # trailing CRLF after the last chunk
                break
            body.extend(self.rfile.read(size))
            self.rfile.readline()  # CRLF after each chunk
        return bytes(body)


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
    # Server emits a single ``X-Repeat: a, b`` line — its value is preserved.
    assert response.headers.get("x-repeat") == "a, b"


def test_protocol_reports_http_1_1(echo_server: str) -> None:
    client = RequestsHttpClient()
    request = Request(method=Method.GET, url=Url.parse(f"{echo_server}/"))
    with client:
        response = client.execute(request)
    # The echo server speaks HTTP/1.1; urllib3 reports version 11.
    assert response.protocol is Protocol.HTTP_1_1


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


class _FixedStatusAdapter(requests.adapters.BaseAdapter):
    """Transport adapter that returns a fixed status with a tracked close."""

    def __init__(self, status_code: int, body: bytes, closed: dict[str, bool]) -> None:
        super().__init__()
        self._status_code = status_code
        self._body = body
        self._closed = closed

    def send(  # signature mirrors BaseAdapter.send
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = self._status_code
        response.reason = "Synthesized"
        response.url = request.url or ""
        response.raw = io.BytesIO(self._body)
        tracker = self._closed
        original_close = response.close

        def _close() -> None:
            tracker["yes"] = True
            original_close()

        # ``requests.Response.close`` releases ``raw``; record the call.
        response.close = _close  # type: ignore[method-assign]
        return response

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


def test_in_range_unregistered_status_is_preserved() -> None:
    """An in-range status the registry doesn't name yields a normal Response."""
    closed = {"yes": False}
    session = requests.Session()
    session.mount("http://", _FixedStatusAdapter(218, b"this is fine", closed))
    client = RequestsHttpClient(session=session)
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with client:
        response = client.execute(request)
        # No error: the live response is preserved, not discarded.
        assert int(response.status) == 218
        assert response.body is not None
        assert response.body.bytes() == b"this is fine"


def test_invalid_status_releases_and_raises() -> None:
    """An out-of-range status code is released and mapped to an error."""
    closed = {"yes": False}
    session = requests.Session()
    session.mount("http://", _FixedStatusAdapter(999, b"", closed))
    client = RequestsHttpClient(session=session)
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with client, pytest.raises(ServiceResponseError):
        client.execute(request)
    assert closed["yes"], "Response should be closed when status mapping fails"


class _BodyFailureAdapter(requests.adapters.BaseAdapter):
    """Returns a 200 whose body stream raises ``exc`` partway through the read."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def send(  # signature mirrors BaseAdapter.send
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.reason = "OK"
        response.url = request.url or ""
        response.raw = io.BytesIO(b"")
        exc = self._exc

        def _raising_iter(chunk_size: int = 1, decode_unicode: bool = False) -> Iterator[bytes]:
            yield b"partial"
            raise exc

        response.iter_content = _raising_iter  # type: ignore[method-assign, assignment]
        return response

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


def test_body_read_chunked_error_maps_to_service_response_error() -> None:
    """A mid-body ``ChunkedEncodingError`` surfaces as ServiceResponseError."""
    session = requests.Session()
    session.mount("http://", _BodyFailureAdapter(requests.exceptions.ChunkedEncodingError("boom")))
    client = RequestsHttpClient(session=session)
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with client:
        response = client.execute(request)
        assert response.body is not None
        with pytest.raises(ServiceResponseError):
            response.body.bytes()


def test_body_read_timeout_maps_to_service_response_timeout_error() -> None:
    """A mid-body ``ReadTimeout`` surfaces as ServiceResponseTimeoutError."""
    session = requests.Session()
    session.mount("http://", _BodyFailureAdapter(requests.exceptions.ReadTimeout("slow")))
    client = RequestsHttpClient(session=session)
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with client:
        response = client.execute(request)
        assert response.body is not None
        with pytest.raises(ServiceResponseTimeoutError):
            response.body.bytes()


class _CapturingAdapter(requests.adapters.BaseAdapter):
    """Adapter that records the prepared request and returns an empty 200."""

    def __init__(self, captured: dict[str, object]) -> None:
        super().__init__()
        self._captured = captured

    def send(  # signature mirrors BaseAdapter.send
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        body = request.body
        if hasattr(body, "read"):
            body = body.read()  # type: ignore[union-attr]
        elif body is not None and not isinstance(body, (bytes, str)):
            body = b"".join(body)  # type: ignore[arg-type]
        self._captured["headers"] = dict(request.headers)
        self._captured["body"] = body
        response = requests.Response()
        response.status_code = 200
        response.reason = "OK"
        response.url = request.url or ""
        response.raw = io.BytesIO(b"")
        return response

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


def _framing_headers(headers: dict[str, str]) -> tuple[bool, bool]:
    lowered = {name.lower() for name in headers}
    return "content-length" in lowered, "transfer-encoding" in lowered


def test_known_length_body_is_framed_not_chunked() -> None:
    """A known-length replayable body goes out with Content-Length, no chunking."""
    captured: dict[str, object] = {}
    session = requests.Session()
    session.mount("http://", _CapturingAdapter(captured))
    client = RequestsHttpClient(session=session)
    payload = b"hello world"
    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/x"),
        body=RequestBody.from_bytes(payload),
    )
    with client:
        client.execute(request)
    headers = captured["headers"]
    assert isinstance(headers, dict)
    has_length, has_chunked = _framing_headers(headers)
    # Exactly one framing header, and it is Content-Length.
    assert has_length and not has_chunked
    assert headers["Content-Length"] == str(len(payload))
    # The body is sent as raw bytes (framed), not as a chunked generator.
    assert captured["body"] == payload


def test_file_body_is_length_framed(tmp_path: pathlib.Path) -> None:
    """A FileRequestBody (replayable, known length) frames with Content-Length."""
    path = tmp_path / "payload.bin"
    payload = b"file contents here"
    path.write_bytes(payload)
    captured: dict[str, object] = {}
    session = requests.Session()
    session.mount("http://", _CapturingAdapter(captured))
    client = RequestsHttpClient(session=session)
    request = Request(
        method=Method.PUT,
        url=Url.parse("http://example.test/x"),
        body=RequestBody.from_file(path),
    )
    with client:
        client.execute(request)
    headers = captured["headers"]
    assert isinstance(headers, dict)
    has_length, has_chunked = _framing_headers(headers)
    assert has_length and not has_chunked
    assert headers["Content-Length"] == str(len(payload))
    assert captured["body"] == payload


def test_streaming_body_is_chunked_not_length_framed() -> None:
    """An unknown-length streaming body goes out chunked, with no Content-Length."""
    captured: dict[str, object] = {}
    session = requests.Session()
    session.mount("http://", _CapturingAdapter(captured))
    client = RequestsHttpClient(session=session)

    def chunks() -> Iterator[bytes]:
        yield b"ab"
        yield b"cd"

    request = Request(
        method=Method.POST,
        url=Url.parse("http://example.test/x"),
        body=RequestBody.from_iter(chunks()),
    )
    with client:
        client.execute(request)
    headers = captured["headers"]
    assert isinstance(headers, dict)
    has_length, has_chunked = _framing_headers(headers)
    # Exactly one framing header, and it is Transfer-Encoding: chunked.
    assert has_chunked and not has_length


def test_never_emits_both_framing_headers() -> None:
    """Neither a sized nor a streaming body may carry both framing headers."""
    payload = b"hello world"

    def chunks() -> Iterator[bytes]:
        yield payload

    bodies = [
        RequestBody.from_bytes(payload),
        RequestBody.from_iter(chunks(), content_length=len(payload)),
        RequestBody.from_iter(chunks()),
    ]
    for body in bodies:
        captured: dict[str, object] = {}
        session = requests.Session()
        session.mount("http://", _CapturingAdapter(captured))
        client = RequestsHttpClient(session=session)
        request = Request(
            method=Method.POST,
            url=Url.parse("http://example.test/x"),
            body=body,
        )
        with client:
            client.execute(request)
        headers = captured["headers"]
        assert isinstance(headers, dict)
        has_length, has_chunked = _framing_headers(headers)
        assert not (has_length and has_chunked), f"both framing headers for {body!r}"


def test_caller_content_length_never_rides_with_chunked() -> None:
    """A caller-set Content-Length must never coexist with chunked framing.

    A request can legitimately carry an explicit ``Content-Length`` header. If
    the body is then streamed (unknown length) ``requests`` adds
    ``Transfer-Encoding: chunked`` without removing that header, producing the
    both-headers / un-framed-body wire bug. A sized body must frame by length;
    an unknown-length body must drop the stale header and chunk cleanly.
    """
    payload = b"hello world"

    def chunks() -> Iterator[bytes]:
        yield payload

    cases = [
        # (body, expect_length, expect_chunked)
        (RequestBody.from_iter(chunks(), content_length=len(payload)), True, False),
        (RequestBody.from_iter(chunks()), False, True),
        (RequestBody.from_stream(io.BytesIO(payload), content_length=len(payload)), True, False),
        (RequestBody.from_stream(io.BytesIO(payload)), False, True),
    ]
    for body, expect_length, expect_chunked in cases:
        captured: dict[str, object] = {}
        session = requests.Session()
        session.mount("http://", _CapturingAdapter(captured))
        client = RequestsHttpClient(session=session)
        request = Request(
            method=Method.POST,
            url=Url.parse("http://example.test/x"),
            body=body,
            # The caller stamps an explicit Content-Length up front.
            headers=Headers([("Content-Length", str(len(payload)))]),
        )
        with client:
            client.execute(request)
        headers = captured["headers"]
        assert isinstance(headers, dict)
        has_length, has_chunked = _framing_headers(headers)
        assert not (has_length and has_chunked), f"both framing headers for {body!r}"
        assert has_length is expect_length and has_chunked is expect_chunked, f"{body!r}"


def test_shared_session_is_not_closed_on_close() -> None:
    """A caller-supplied Session is left open when the adapter closes."""
    session = requests.Session()
    calls = {"closed": False}
    original_close = session.close

    def _close() -> None:
        calls["closed"] = True
        original_close()

    session.close = _close  # type: ignore[method-assign]
    client = RequestsHttpClient(session=session)
    client.close()
    # The session is borrowed; closing the client must not tear it down.
    assert not calls["closed"], "shared session must not be closed by client.close()"


def test_owned_session_is_closed_on_close() -> None:
    """A session the client created is closed on the adapter's close."""
    client = RequestsHttpClient()
    # Reaching into the private session is intentional: this test asserts the
    # ownership teardown contract.
    session = client._session
    calls = {"closed": False}
    original_close = session.close

    def _close() -> None:
        calls["closed"] = True
        original_close()

    session.close = _close  # type: ignore[method-assign]
    client.close()
    assert calls["closed"], "owned session should be closed"


class _DuplicateHeaderAdapter(requests.adapters.BaseAdapter):
    """Adapter returning a response with repeated headers and a chosen body."""

    def __init__(self, response: requests.Response) -> None:
        super().__init__()
        self._response = response

    def send(  # signature mirrors BaseAdapter.send
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        self._response.url = request.url or ""
        return self._response

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


def _urllib3_response(headers: list[tuple[str, str]], body: bytes) -> requests.Response:
    """Build a streamed ``requests.Response`` backed by a real urllib3 response."""
    from urllib3 import HTTPHeaderDict
    from urllib3.response import HTTPResponse

    raw = HTTPResponse(
        body=io.BytesIO(body),
        headers=HTTPHeaderDict(headers),
        status=200,
        reason="OK",
        version=11,
        preload_content=False,
    )
    response = requests.Response()
    response.status_code = 200
    response.reason = "OK"
    response.raw = raw
    response.headers = requests.structures.CaseInsensitiveDict(raw.headers)
    return response


def test_repeated_set_cookie_headers_preserved() -> None:
    """Repeated ``Set-Cookie`` lines survive instead of being comma-joined."""
    raw_response = _urllib3_response(
        [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("Content-Length", "2")],
        b"{}",
    )
    session = requests.Session()
    session.mount("http://", _DuplicateHeaderAdapter(raw_response))
    client = RequestsHttpClient(session=session)
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with client:
        response = client.execute(request)
    cookies = response.headers.values("set-cookie")
    assert list(cookies) == ["a=1", "b=2"]


def test_content_length_dropped_when_content_encoding_present() -> None:
    """A compressed response must not propagate the encoded Content-Length."""
    # ``requests`` decodes the body, so the upstream Content-Length (encoded
    # size) would misdescribe the decoded stream the SDK exposes.
    raw_response = _urllib3_response(
        [("Content-Encoding", "gzip"), ("Content-Length", "20")],
        b"decoded plaintext body",
    )
    session = requests.Session()
    session.mount("http://", _DuplicateHeaderAdapter(raw_response))
    client = RequestsHttpClient(session=session)
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with client:
        response = client.execute(request)
        body = response.body
        assert body is not None
        # The wrong (encoded) length is dropped; the body itself still reads.
        assert body.content_length() == -1


def test_content_length_propagated_without_encoding() -> None:
    """Without Content-Encoding the response Content-Length is surfaced."""
    raw_response = _urllib3_response([("Content-Length", "5")], b"hello")
    session = requests.Session()
    session.mount("http://", _DuplicateHeaderAdapter(raw_response))
    client = RequestsHttpClient(session=session)
    request = Request(method=Method.GET, url=Url.parse("http://example.test/"))
    with client:
        response = client.execute(request)
        body = response.body
        assert body is not None
        assert body.content_length() == 5
