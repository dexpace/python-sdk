# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``Request`` immutability and the ``with_*`` helpers."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import Headers, Url
from dexpace.sdk.core.http.common.http_header_name import AUTHORIZATION, CONTENT_TYPE
from dexpace.sdk.core.http.request import Method, Request, RequestBody


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


def test_with_method_returns_new_instance() -> None:
    r = _request()
    updated = r.with_method(Method.POST)
    assert updated.method is Method.POST
    assert r.method is Method.GET


def test_with_url_accepts_string() -> None:
    r = _request()
    new = r.with_url("https://other.example.com/")
    assert new.url == Url.parse("https://other.example.com/")
    assert r.url == Url.parse("https://example.com/")


def test_with_url_accepts_url_instance() -> None:
    r = _request()
    target = Url.parse("https://other.example.com/")
    new = r.with_url(target)
    assert new.url is target


def test_url_is_url_type() -> None:
    r = _request()
    assert isinstance(r.url, Url)


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


def _post_request() -> Request:
    return Request(method=Method.POST, url=Url.parse("https://example.com/"))


@pytest.mark.req("HTTP-6")
def test_with_body() -> None:
    body = RequestBody.from_string("payload")
    r = _post_request().with_body(body)
    assert r.body is body


@pytest.mark.req("HTTP-1", "XCUT-15")
def test_frozen() -> None:
    from dataclasses import FrozenInstanceError

    r = _request()
    with pytest.raises(FrozenInstanceError):
        r.url = Url.parse("https://x/")  # type: ignore[misc]


@pytest.mark.req("HTTP-6")
def test_default_headers_are_empty() -> None:
    r = _request()
    assert isinstance(r.headers, Headers)
    assert len(r.headers) == 0


# --- Validation grammar -----------------------------------------------------


@pytest.mark.req("HTTP-4")
def test_missing_url_raises_field_named_error() -> None:
    with pytest.raises(ValueError, match=r"^url is required$"):
        Request(method=Method.GET, url=None)  # type: ignore[arg-type]


@pytest.mark.req("HTTP-4")
def test_missing_method_raises_field_named_error() -> None:
    with pytest.raises(ValueError, match=r"^method is required$"):
        Request(method=None, url=Url.parse("https://example.com/"))  # type: ignore[arg-type]


def test_body_on_get_is_rejected() -> None:
    body = RequestBody.from_string("payload")
    with pytest.raises(ValueError, match="body is not permitted on a GET request"):
        Request(method=Method.GET, url=Url.parse("https://example.com/"), body=body)


def test_body_on_head_is_rejected() -> None:
    body = RequestBody.from_string("payload")
    with pytest.raises(ValueError, match="body is not permitted on a HEAD request"):
        Request(method=Method.HEAD, url=Url.parse("https://example.com/"), body=body)


def test_with_body_on_get_is_rejected() -> None:
    body = RequestBody.from_string("payload")
    with pytest.raises(ValueError, match="body is not permitted on a GET request"):
        _request().with_body(body)


def test_replace_revalidates_body_on_get() -> None:
    from dataclasses import replace

    post = _post_request().with_body(RequestBody.from_string("payload"))
    with pytest.raises(ValueError, match="body is not permitted on a GET request"):
        replace(post, method=Method.GET)


def test_with_method_to_get_drops_body() -> None:
    post = _post_request().with_body(RequestBody.from_string("payload"))
    switched = post.with_method(Method.GET)
    assert switched.method is Method.GET
    assert switched.body is None
    # The original is untouched (value semantics).
    assert post.body is not None


def test_with_method_between_body_methods_keeps_body() -> None:
    body = RequestBody.from_string("payload")
    post = _post_request().with_body(body)
    switched = post.with_method(Method.PUT)
    assert switched.method is Method.PUT
    assert switched.body is body


# --- Per-call boundary ------------------------------------------------------


@pytest.mark.req("HTTP-35")
def test_timeout_zero_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        Request(method=Method.GET, url=Url.parse("https://example.com/"), timeout=0)


def test_timeout_negative_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        Request(method=Method.GET, url=Url.parse("https://example.com/"), timeout=-1.0)


def test_positive_timeout_is_accepted() -> None:
    r = Request(method=Method.GET, url=Url.parse("https://example.com/"), timeout=2.5)
    assert r.timeout == 2.5


@pytest.mark.req("HTTP-35")
def test_negative_retries_is_rejected() -> None:
    with pytest.raises(ValueError, match="retries must not be negative"):
        Request(method=Method.GET, url=Url.parse("https://example.com/"), retries=-1)


@pytest.mark.req("HTTP-35")
def test_zero_retries_means_no_retries() -> None:
    r = Request(method=Method.GET, url=Url.parse("https://example.com/"), retries=0)
    assert r.retries == 0


# --- Repr redaction ---------------------------------------------------------


def test_repr_redacts_url_and_header_credentials() -> None:
    r = Request(
        method=Method.GET,
        url=Url.parse("https://user:secret@example.com/path?token=abc&page=2"),
    ).with_header(AUTHORIZATION, "Bearer super-secret")
    rendered = repr(r)
    assert "secret" not in rendered  # covers both userinfo and the bearer token
    assert "abc" not in rendered
    assert "REDACTED" in rendered
    assert "page=2" in rendered  # an allow-listed query key survives
