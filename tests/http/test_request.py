"""Tests for ``Request`` immutability and the ``with_*`` helpers."""
from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import Headers
from dexpace.sdk.core.http.common.http_header_name import AUTHORIZATION, CONTENT_TYPE
from dexpace.sdk.core.http.request import Method, Request, RequestBody


def _request() -> Request:
    return Request(method=Method.GET, url="https://example.com/")


def test_with_method_returns_new_instance() -> None:
    r = _request()
    updated = r.with_method(Method.POST)
    assert updated.method is Method.POST
    assert r.method is Method.GET


def test_with_url_returns_new_instance() -> None:
    r = _request()
    new = r.with_url("https://other.example.com/")
    assert new.url == "https://other.example.com/"
    assert r.url == "https://example.com/"


def test_with_header_accepts_http_header_name() -> None:
    r = _request().with_header(CONTENT_TYPE, "application/json")
    assert r.headers.get(CONTENT_TYPE) == "application/json"


def test_with_header_accepts_str() -> None:
    r = _request().with_header("X-Custom", "abc")
    assert r.headers.get("x-custom") == "abc"


def test_with_added_header_appends_to_list() -> None:
    r = _request().with_added_header("Vary", "Accept")
    r = r.with_added_header("Vary", "Accept-Encoding")
    assert r.headers.values("vary") == ("Accept", "Accept-Encoding")


def test_without_header_drops_it() -> None:
    r = _request().with_header(AUTHORIZATION, "Bearer abc").without_header(AUTHORIZATION)
    assert AUTHORIZATION not in r.headers


def test_with_body() -> None:
    body = RequestBody.from_string("payload")
    r = _request().with_body(body)
    assert r.body is body


def test_frozen() -> None:
    from dataclasses import FrozenInstanceError

    r = _request()
    with pytest.raises(FrozenInstanceError):
        r.url = "x"  # type: ignore[misc]


def test_default_headers_are_empty() -> None:
    r = _request()
    assert isinstance(r.headers, Headers)
    assert len(r.headers) == 0
