# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``Response`` immutability, parsing, reprs, and close semantics."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, dataclass

import pytest

from dexpace.sdk.core.http.common import Headers, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, ResponseBody, Status


@dataclass(frozen=True, slots=True)
class _Pet:
    name: str


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


def _response(body: ResponseBody | None = None) -> Response:
    return Response(
        request=_request(),
        protocol=Protocol.HTTP_1_1,
        status=Status.OK,
        body=body,
    )


class _CountingBody(ResponseBody):
    """Response body that counts how many times it is read (drained)."""

    __slots__ = ("_data", "_lock", "closed", "reads")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._lock = threading.Lock()
        self.reads = 0
        self.closed = False

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return len(self._data)

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        with self._lock:
            self.reads += 1
        # Widen the race window: while the parse latch holds its lock during
        # this single drain, every other thread is parked at the latch.
        time.sleep(0.02)
        yield self._data
        self.close()

    def close(self) -> None:
        self.closed = True


class _TrackingBody(ResponseBody):
    """Response body that records whether it was read and/or closed."""

    __slots__ = ("closed", "read")

    def __init__(self) -> None:
        self.closed = False
        self.read = False

    def media_type(self) -> None:
        return None

    def content_length(self) -> int:
        return 0

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        self.read = True
        yield b""
        self.close()

    def close(self) -> None:
        self.closed = True


def test_is_success_property() -> None:
    r = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.OK)
    assert r.is_success
    r2 = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.NOT_FOUND)
    assert not r2.is_success


@pytest.mark.req("HTTP-11")
def test_status_group_properties() -> None:
    req = _request()
    redirect = Response(request=req, protocol=Protocol.HTTP_1_1, status=Status.FOUND)
    assert redirect.is_redirect
    assert not redirect.is_client_error
    assert not redirect.is_server_error

    client_err = Response(request=req, protocol=Protocol.HTTP_1_1, status=Status.NOT_FOUND)
    assert client_err.is_client_error
    assert not client_err.is_server_error
    assert not client_err.is_redirect

    server_err = Response(
        request=req, protocol=Protocol.HTTP_1_1, status=Status.INTERNAL_SERVER_ERROR
    )
    assert server_err.is_server_error
    assert not server_err.is_client_error


@pytest.mark.req("HTTP-43")
def test_close_idempotent_with_no_body() -> None:
    r = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.OK)
    r.close()
    r.close()


def test_context_manager_closes_body() -> None:
    body = ResponseBody.from_bytes(b"x")
    r = Response(
        request=_request(),
        protocol=Protocol.HTTP_1_1,
        status=Status.OK,
        body=body,
    )
    with r as same:
        assert same is r
    # Closing via the body's _SourceResponseBody marks _closed=True; double-close is fine.
    r.close()


def test_with_header() -> None:
    r = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.OK)
    updated = r.with_header("X-Trace", "abc")
    assert updated.headers.get("x-trace") == "abc"
    assert len(r.headers) == 0


def test_with_status() -> None:
    r = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.OK)
    updated = r.with_status(Status.NOT_FOUND)
    assert updated.status is Status.NOT_FOUND
    assert r.status is Status.OK


@pytest.mark.req("HTTP-6")
def test_default_headers_empty() -> None:
    r = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.OK)
    assert isinstance(r.headers, Headers)
    assert len(r.headers) == 0


# --- Validation grammar -----------------------------------------------------


@pytest.mark.req("HTTP-1", "XCUT-15")
def test_frozen_rejects_attribute_assignment() -> None:
    resp = _response()
    with pytest.raises(FrozenInstanceError):
        resp.status = Status.NOT_FOUND  # type: ignore[misc]


@pytest.mark.req("HTTP-4")
def test_missing_request_raises_field_named_error() -> None:
    with pytest.raises(ValueError, match=r"^request is required$"):
        Response(request=None, protocol=Protocol.HTTP_1_1, status=Status.OK)  # type: ignore[arg-type]


def test_missing_protocol_raises_field_named_error() -> None:
    with pytest.raises(ValueError, match=r"^protocol is required$"):
        Response(request=_request(), protocol=None, status=Status.OK)  # type: ignore[arg-type]


def test_missing_status_raises_field_named_error() -> None:
    with pytest.raises(ValueError, match=r"^status is required$"):
        Response(request=_request(), protocol=Protocol.HTTP_1_1, status=None)  # type: ignore[arg-type]


# --- parse: decoding + memoizing latch --------------------------------------


def test_parse_decodes_json_body_with_default_serde() -> None:
    resp = _response(ResponseBody.from_bytes(b'{"a": 1, "b": 2}'))
    assert resp.parse(dict[str, int]) == {"a": 1, "b": 2}


def test_parse_decodes_generic_list() -> None:
    resp = _response(ResponseBody.from_bytes(b"[1, 2, 3]"))
    assert resp.parse(list[int]) == [1, 2, 3]


def test_parse_decodes_list_of_dataclasses() -> None:
    resp = _response(ResponseBody.from_bytes(b'[{"name": "rex"}, {"name": "mia"}]'))
    assert resp.parse(list[_Pet]) == [_Pet("rex"), _Pet("mia")]


def test_parse_without_body_raises() -> None:
    resp = _response()
    with pytest.raises(ValueError, match="no body to parse"):
        resp.parse(list[int])


def test_parse_memoizes_and_reads_body_once() -> None:
    body = _CountingBody(b"[1, 2, 3]")
    resp = _response(body)
    first = resp.parse(list[int])
    second = resp.parse(list[int])
    assert first == [1, 2, 3]
    assert second is first  # cached object, not re-decoded
    assert body.reads == 1


def test_replace_resets_the_parse_latch() -> None:
    resp = _response(ResponseBody.from_bytes(b"[1]"))
    assert resp.parse(list[int]) == [1]
    # A derived response carries its own fresh, unlatched cache (state is not
    # aliased across ``replace``).
    resp2 = resp.with_body(ResponseBody.from_bytes(b"[2]"))
    assert resp2.parse(list[int]) == [2]


@pytest.mark.req("HTTP-45")
def test_parse_latch_deserializes_exactly_once_under_concurrent_first_access() -> None:
    thread_count = 24
    body = _CountingBody(b"[1, 2, 3]")
    resp = _response(body)
    results: list[list[int]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(thread_count)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(resp.parse(list[int]))
        except Exception as exc:  # pragma: no cover - recorded for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert body.reads == 1  # exactly one deserialization despite the race
    assert len(results) == thread_count
    first = results[0]
    assert all(result is first for result in results)  # every caller shares it
    assert first == [1, 2, 3]


# --- Repr redaction ---------------------------------------------------------


def test_repr_redacts_url_and_credential_headers() -> None:
    req = Request(
        method=Method.GET,
        url=Url.parse("https://user:secret@example.com/x?token=abc&page=2"),
    ).with_header("Authorization", "Bearer super-secret")
    resp = Response(request=req, protocol=Protocol.HTTP_1_1, status=Status.OK).with_header(
        "Set-Cookie", "sid=topsecret"
    )
    rendered = repr(resp)
    assert "secret" not in rendered  # userinfo, bearer token, and cookie
    assert "abc" not in rendered  # non-allow-listed query value
    assert "REDACTED" in rendered
    assert "page=2" in rendered  # allow-listed query key survives


def test_repr_omits_the_private_parse_latch() -> None:
    rendered = repr(_response(ResponseBody.from_bytes(b"[]")))
    assert "_latch" not in rendered
    assert "_ParseLatch" not in rendered


# --- Close-once semantics across paths --------------------------------------


@pytest.mark.req("BODY-15", "HTTP-41")
def test_close_releases_body_without_reading() -> None:
    body = _TrackingBody()
    _response(body).close()
    assert body.closed
    assert not body.read  # connection released even though nothing was read


@pytest.mark.req("BODY-15", "HTTP-41")
def test_close_is_idempotent() -> None:
    body = _TrackingBody()
    resp = _response(body)
    resp.close()
    resp.close()
    assert body.closed  # repeated close does not raise


@pytest.mark.req("HTTP-43")
def test_context_manager_closes_on_exit() -> None:
    body = _TrackingBody()
    resp = _response(body)
    with resp as same:
        assert same is resp
    assert body.closed


@pytest.mark.req("HTTP-43")
def test_context_manager_closes_on_exception() -> None:
    body = _TrackingBody()
    with pytest.raises(RuntimeError, match="boom"), _response(body):
        raise RuntimeError("boom")
    assert body.closed
