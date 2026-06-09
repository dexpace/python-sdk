# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Serialization snapshot tests (Q3).

Wire-format output drifts silently — a change in header canonicalization, URL
serialization, or body chunk boundaries can slip through review unnoticed.
These tests pin the exact bytes / strings a known request serializes to. Each
snapshot is deliberately narrow and paired with a behavioral assertion so a
failure points at *which* contract moved, not merely that some string changed.
"""

from __future__ import annotations

from dexpace.sdk.core.http.common import Headers, QueryParams, Url
from dexpace.sdk.core.http.request import Method, Request, RequestBody

# ----- Headers canonicalization -------------------------------------------


def test_headers_canonicalise_names_to_lower_case() -> None:
    headers = Headers(
        [
            ("Content-Type", "application/json"),
            ("X-Trace-Id", "abc123"),
        ]
    )
    # Names are stored lower-cased; values are preserved verbatim. The exact
    # ``items()`` tuple is the wire-canonical snapshot.
    assert headers.items() == (
        ("content-type", ("application/json",)),
        ("x-trace-id", ("abc123",)),
    )
    # Behavioral pairing: the canonicalized form is still looked up by any case.
    assert headers.get("CONTENT-TYPE") == "application/json"


def test_headers_preserve_multi_value_order() -> None:
    headers = Headers([("Set-Cookie", ["a=1", "b=2"])])
    assert headers.items() == (("set-cookie", ("a=1", "b=2")),)
    # Behavioral pairing: ``get`` returns the first value, ``values`` all of them.
    assert headers.get("set-cookie") == "a=1"
    assert headers.values("Set-Cookie") == ("a=1", "b=2")


def test_headers_repr_redacts_sensitive_values() -> None:
    headers = Headers([("Authorization", "Bearer secret-token"), ("Accept", "*/*")])
    # The repr snapshot must never leak the credential.
    assert repr(headers) == "Headers({'authorization': ['[REDACTED]'], 'accept': ['*/*']})"


# ----- URL serialization --------------------------------------------------


def test_url_round_trips_to_wire_form() -> None:
    raw = "https://api.example.com/v1/users?b=2&a=1&a=3#frag"
    url = Url.parse(raw)
    assert str(url) == raw
    # Behavioral pairing: query order and multiplicity survive the parse.
    assert url.query.flatten() == (("b", "2"), ("a", "1"), ("a", "3"))


def test_url_serialization_encodes_space_and_ampersand_in_query() -> None:
    url = Url(
        scheme="https",
        host="api.example.com",
        path="/search",
        query=QueryParams({"q": "a b", "tag": ["x", "y"]}),
    )
    assert str(url) == "https://api.example.com/search?q=a+b&tag=x&tag=y"
    # ``QueryParams.encode`` uses percent-encoding for the space (RFC 3986),
    # whereas the full-URL serializer renders the form-style ``+``.
    assert url.query.encode() == "q=a%20b&tag=x&tag=y"


def test_url_serialization_preserves_explicit_port() -> None:
    url = Url.parse("https://api.example.com:8443/p?x=1")
    assert str(url) == "https://api.example.com:8443/p?x=1"
    assert url.port == 8443


def test_url_str_redacts_userinfo_but_wire_form_keeps_it() -> None:
    url = Url.parse("https://user:pw@host.example/secret")
    # ``str`` drops credentials to avoid leaking them through logs ...
    assert str(url) == "https://host.example/secret"
    # ... while ``wire_form`` keeps them for an actual request line.
    assert url.wire_form() == "https://user:pw@host.example/secret"


# ----- body chunking ------------------------------------------------------


def test_bytes_body_chunks_on_exact_boundaries() -> None:
    body = RequestBody.from_bytes(b"0123456789abcdef")
    assert list(body.iter_bytes(4)) == [b"0123", b"4567", b"89ab", b"cdef"]
    # A non-divisor chunk size yields a short final chunk.
    assert list(body.iter_bytes(5)) == [b"01234", b"56789", b"abcde", b"f"]
    # Behavioral pairing: chunking is non-destructive — content_length and the
    # joined bytes are stable across reads (the body is replayable).
    assert body.content_length() == 16
    assert b"".join(body.iter_bytes(4)) == b"0123456789abcdef"


def test_string_body_encodes_utf8_and_reports_byte_length() -> None:
    body = RequestBody.from_string("héllo")
    assert b"".join(body.iter_bytes()) == b"h\xc3\xa9llo"
    # ``content_length`` is the byte count, not the character count.
    assert body.content_length() == 6


def test_form_body_url_encodes_fields_and_sets_media_type() -> None:
    body = RequestBody.from_form({"name": "a b", "q": "x&y"})
    assert b"".join(body.iter_bytes()) == b"name=a%20b&q=x%26y"
    media_type = body.media_type()
    assert media_type is not None
    assert str(media_type) == "application/x-www-form-urlencoded"


def test_request_carries_its_serialized_components() -> None:
    request = Request(
        method=Method.POST,
        url=Url.parse("https://api.example.com/orders?dry_run=true"),
        headers=Headers({"Content-Type": "application/json"}),
        body=RequestBody.from_string('{"id":1}'),
    )
    assert str(request.url) == "https://api.example.com/orders?dry_run=true"
    assert request.headers.get("content-type") == "application/json"
    assert request.body is not None
    assert b"".join(request.body.iter_bytes()) == b'{"id":1}'
