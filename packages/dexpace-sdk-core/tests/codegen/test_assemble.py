# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``assemble`` — turning a declarative operation into a ``Request``."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.codegen import Operation, OperationInput, assemble
from dexpace.sdk.core.http.common import Headers, QueryParams, Url
from dexpace.sdk.core.http.request import Method, RequestBody


def _op(path: str, method: Method = Method.GET, **kw: object) -> Operation:
    return Operation(name="advisory", method=method, path=path, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Signed / SAS-token base URL: existing query preserved, op query appended.    #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-27")
def test_sas_query_preserved_and_operation_query_appended() -> None:
    base = (
        "https://acct.blob.core.windows.net/container/blob"
        "?sv=2021-08-06&sig=abc%2Bdef%2F123%3D&se=2030-01-01"
    )
    inp = OperationInput(query=QueryParams([("snapshot", "2020-01-01"), ("comp", "block")]))
    req = assemble(_op(""), inp, base)

    # The base query survives byte-for-byte (RFC 3986 wire form) and the
    # operation's own parameters are appended strictly after it.
    assert str(req.url) == (
        "https://acct.blob.core.windows.net/container/blob"
        "?sv=2021-08-06&sig=abc%2Bdef%2F123%3D&se=2030-01-01"
        "&snapshot=2020-01-01&comp=block"
    )


def test_sas_query_pairs_ordered_base_then_operation() -> None:
    base = "https://h.example.com/x?token=t1&token=t2"
    inp = OperationInput(query=QueryParams([("page", "2")]))
    req = assemble(_op("/data"), inp, base)
    assert req.url.query.flatten() == (
        ("token", "t1"),
        ("token", "t2"),
        ("page", "2"),
    )


def test_base_query_preserved_when_operation_adds_none() -> None:
    req = assemble(_op("/data"), OperationInput(), "https://h.example.com/?token=abc")
    assert req.url.query.flatten() == (("token", "abc"),)


# --------------------------------------------------------------------------- #
# Path parameters: encoded as a single segment, missing ones are named.        #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-27")
def test_path_value_with_slash_stays_single_segment() -> None:
    inp = OperationInput(path_params={"path": "a/b/c"})
    req = assemble(_op("/files/{path}/meta"), inp, "https://api.example.com")
    # The literal '/' is percent-encoded, so it never introduces a new segment.
    assert req.url.path == "/files/a%2Fb%2Fc/meta"
    assert req.url.path.split("/") == ["", "files", "a%2Fb%2Fc", "meta"]


def test_path_value_encodes_space_and_non_ascii() -> None:
    inp = OperationInput(path_params={"term": "hello world/ü"})
    req = assemble(_op("/search/{term}"), inp, "https://api.example.com")
    assert req.url.path == "/search/hello%20world%2F%C3%BC"


def test_all_placeholders_substituted() -> None:
    inp = OperationInput(path_params={"userId": "42", "itemId": "99"})
    req = assemble(_op("/users/{userId}/items/{itemId}"), inp, "https://api.example.com")
    assert req.url.path == "/users/42/items/99"


@pytest.mark.req("SEAM-27")
def test_missing_placeholder_is_named_in_error() -> None:
    inp = OperationInput(path_params={"userId": "42"})
    with pytest.raises(KeyError) as excinfo:
        assemble(_op("/users/{userId}/items/{itemId}"), inp, "https://api.example.com")
    assert "itemId" in str(excinfo.value)


def test_path_param_value_is_not_leaked_when_placeholder_missing() -> None:
    # Only the missing name is reported; supplied (possibly secret) values must
    # not appear in the error message.
    inp = OperationInput(path_params={"userId": "s3cr3t-token"})
    with pytest.raises(KeyError) as excinfo:
        assemble(_op("/users/{userId}/items/{itemId}"), inp, "https://api.example.com")
    assert "s3cr3t-token" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Trailing-slash normalisation.                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-27")
def test_trailing_slash_on_base_does_not_double() -> None:
    req = assemble(_op("/users"), OperationInput(), "https://api.example.com/v1/")
    assert req.url.path == "/v1/users"


def test_operation_path_without_leading_slash_joins_cleanly() -> None:
    req = assemble(_op("users"), OperationInput(), "https://api.example.com/v1")
    assert req.url.path == "/v1/users"


def test_empty_operation_path_keeps_base_path() -> None:
    req = assemble(_op(""), OperationInput(), "https://api.example.com/v1")
    assert req.url.path == "/v1"


def test_trailing_slash_on_operation_path_preserved() -> None:
    req = assemble(_op("/users/"), OperationInput(), "https://api.example.com")
    assert req.url.path == "/users/"


# --------------------------------------------------------------------------- #
# Base URL validation.                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-27")
def test_base_url_with_fragment_is_rejected() -> None:
    with pytest.raises(ValueError, match="fragment"):
        assemble(_op("/x"), OperationInput(), "https://api.example.com/#section")


def test_malformed_base_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        assemble(_op("/x"), OperationInput(), "not-a-url")


def test_base_url_accepts_url_instance() -> None:
    base = Url.parse("https://api.example.com/v1?k=v")
    req = assemble(_op("/users"), OperationInput(), base)
    assert str(req.url) == "https://api.example.com/v1/users?k=v"


# --------------------------------------------------------------------------- #
# Method, headers, and body pass-through.                                      #
# --------------------------------------------------------------------------- #


def test_method_is_taken_from_operation() -> None:
    req = assemble(_op("/items", method=Method.POST), OperationInput(), "https://h.example.com")
    assert req.method is Method.POST


def test_static_and_per_call_headers_are_merged() -> None:
    op = _op("/", headers=Headers({"accept": "application/json"}))
    inp = OperationInput(headers=Headers({"x-request-id": "abc"}))
    req = assemble(op, inp, "https://h.example.com")
    assert req.headers.get("accept") == "application/json"
    assert req.headers.get("x-request-id") == "abc"


def test_per_call_header_overrides_static_header() -> None:
    op = _op("/", headers=Headers({"accept": "application/json"}))
    inp = OperationInput(headers=Headers({"accept": "text/plain"}))
    req = assemble(op, inp, "https://h.example.com")
    assert req.headers.values("accept") == ("text/plain",)


@pytest.mark.req("SEAM-26")
def test_body_is_carried_through_unchanged() -> None:
    body = RequestBody.from_string("hello")
    req = assemble(
        _op("/items", method=Method.POST), OperationInput(body=body), "https://h.example.com"
    )
    assert req.body is body


# --------------------------------------------------------------------------- #
# ``Operation.name`` has zero effect on the assembled request.                 #
# --------------------------------------------------------------------------- #


@pytest.mark.req("SEAM-28")
def test_operation_name_has_no_effect_on_assembly() -> None:
    a = Operation(name="getUser", method=Method.GET, path="/users/{id}")
    b = Operation(name="retrieveUserById", method=Method.GET, path="/users/{id}")
    inp = OperationInput(path_params={"id": "7"}, query=QueryParams([("q", "x")]))
    ra = assemble(a, inp, "https://api.example.com/v1")
    rb = assemble(b, inp, "https://api.example.com/v1")
    assert ra == rb
    assert str(ra.url) == str(rb.url)
    assert ra.headers == rb.headers
    assert ra.method is rb.method
