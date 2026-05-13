"""Tests for ``Response`` immutability and context-manager behaviour."""

from __future__ import annotations

from dexpace.sdk.core.http.common import Headers, Protocol, Url
from dexpace.sdk.core.http.request import Method, Request
from dexpace.sdk.core.http.response import Response, ResponseBody, Status


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


def test_is_success_property() -> None:
    r = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.OK)
    assert r.is_success
    r2 = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.NOT_FOUND)
    assert not r2.is_success


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


def test_default_headers_empty() -> None:
    r = Response(request=_request(), protocol=Protocol.HTTP_1_1, status=Status.OK)
    assert isinstance(r.headers, Headers)
    assert len(r.headers) == 0
