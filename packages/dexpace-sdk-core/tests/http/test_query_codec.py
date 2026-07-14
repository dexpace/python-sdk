# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Conformance + property tests for the pure RFC 3986 query codec.

The ``query_codec`` (``HTTP-29``) family of the golden wire-exactness corpus is
exercised two ways: directly against the pure, furl-free codec module
(``render_query`` / ``parse_query``) and through the public ``QueryParams`` /
``Url`` API that calls into it. Both must agree with the checked-in byte
fixtures, proving the wire-exact rendering path never routes through furl (whose
normalisations re-encode ``%20`` to ``+``).

A ``hypothesis`` property pins ``parse_query(render_query(pairs)) == pairs`` as
an exact inverse over arbitrary UTF-8 pairs, complementing the enumerated
corpus.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpace.sdk.core.http.common import QueryParams, Url
from dexpace.sdk.core.http.common._query_codec import parse_query, render_query

from ..golden_corpus import QueryCodecVector, load_query_codec

_VECTORS = load_query_codec()

# The corpus tags each vector with which relationships it pins; parametrise each
# check over exactly the applicable subset so every case makes a real assertion
# (a parse-only vector such as ``parse-literal-plus-not-space`` deliberately does
# not render back to its wire form, and is not a render case).
_RENDER_VECTORS = [v for v in _VECTORS if v.render_matches]
_PARSE_VECTORS = [v for v in _VECTORS if v.parse_matches]
_ROUND_TRIP_VECTORS = [v for v in _VECTORS if v.round_trippable]


def _decoded_params(vector: QueryCodecVector) -> list[tuple[str, str]]:
    """The vector's params as decoded ``(name, value)`` string pairs."""
    return [(name.decode(), value.decode()) for name, value in vector.params]


# ----- golden corpus: directly against the pure codec module ---------------


@pytest.mark.req("HTTP-29")
@pytest.mark.parametrize("vector", _RENDER_VECTORS, ids=lambda v: v.id)
def test_pure_render_matches_golden(vector: QueryCodecVector) -> None:
    """Strict render of the params equals the wire query, byte for byte."""
    assert render_query(_decoded_params(vector)) == vector.query.decode("ascii")


@pytest.mark.req("HTTP-29")
@pytest.mark.parametrize("vector", _PARSE_VECTORS, ids=lambda v: v.id)
def test_pure_parse_matches_golden(vector: QueryCodecVector) -> None:
    """Lenient parse of the wire query equals the params (order preserved)."""
    assert parse_query(vector.query.decode("ascii")) == _decoded_params(vector)


@pytest.mark.req("HTTP-29")
@pytest.mark.parametrize("vector", _ROUND_TRIP_VECTORS, ids=lambda v: v.id)
def test_pure_round_trip_is_exact_inverse(vector: QueryCodecVector) -> None:
    """Where the vector claims it, render then parse is the identity."""
    params = _decoded_params(vector)
    assert parse_query(render_query(params)) == params


# ----- golden corpus: through the public QueryParams API -------------------


@pytest.mark.req("HTTP-29")
@pytest.mark.parametrize("vector", _RENDER_VECTORS, ids=lambda v: v.id)
def test_queryparams_encode_matches_golden(vector: QueryCodecVector) -> None:
    """``QueryParams(...).encode()`` renders the wire query byte for byte."""
    params = QueryParams(_decoded_params(vector))
    assert params.encode() == vector.query.decode("ascii")


@pytest.mark.req("HTTP-29")
@pytest.mark.parametrize("vector", _PARSE_VECTORS, ids=lambda v: v.id)
def test_queryparams_parse_matches_golden(vector: QueryCodecVector) -> None:
    """``QueryParams.parse(...)`` flattens back to the params (order preserved)."""
    parsed = QueryParams.parse(vector.query.decode("ascii"))
    assert list(parsed.flatten()) == _decoded_params(vector)


# ----- named edge cases (the traps the codec must not fall into) -----------


def test_space_renders_as_pct20_never_plus() -> None:
    assert render_query([("q", "a b")]) == "q=a%20b"
    assert QueryParams([("q", "a b")]).encode() == "q=a%20b"


def test_literal_plus_renders_as_pct2b() -> None:
    assert render_query([("q", "a+b")]) == "q=a%2Bb"
    assert QueryParams([("q", "a+b")]).encode() == "q=a%2Bb"


def test_wire_plus_parses_as_literal_plus_not_space() -> None:
    # RFC 3986 lenient parse: a wire '+' is a literal '+', NOT form-decoded.
    assert parse_query("q=a+b") == [("q", "a+b")]
    assert QueryParams.parse("q=a+b").get("q") == "a+b"


def test_pct20_parses_as_space() -> None:
    assert parse_query("q=a%20b") == [("q", "a b")]
    assert QueryParams.parse("q=a%20b").get("q") == "a b"


def test_valueless_flag_is_present_empty_string() -> None:
    # A present-but-empty value renders 'flag=' and is distinct from absent.
    assert render_query([("flag", "")]) == "flag="
    assert parse_query("flag=") == [("flag", "")]


def test_bare_flag_parses_to_present_empty_string() -> None:
    # A bare flag with no '=' is a present empty string, distinct from absent.
    assert parse_query("flag") == [("flag", "")]
    parsed = QueryParams.parse("flag")
    assert "flag" in parsed
    assert parsed.get("flag") == ""


def test_reserved_chars_in_value_are_percent_encoded() -> None:
    assert render_query([("q", "a/b?c#d&e=f")]) == "q=a%2Fb%3Fc%23d%26e%3Df"


def test_unreserved_set_is_emitted_verbatim() -> None:
    assert render_query([("q", "AZaz09-._~")]) == "q=AZaz09-._~"


def test_non_ascii_is_utf8_then_percent_encoded() -> None:
    assert render_query([("name", "café")]) == "name=caf%C3%A9"


# ----- order sensitivity ---------------------------------------------------


def test_render_preserves_insertion_order_verbatim() -> None:
    assert render_query([("a", "1"), ("b", "2")]) == "a=1&b=2"
    assert render_query([("b", "2"), ("a", "1")]) == "b=2&a=1"


def test_queryparams_equality_is_order_sensitive() -> None:
    ab = QueryParams([("a", "1"), ("b", "2")])
    ba = QueryParams([("b", "2"), ("a", "1")])
    assert ab != ba
    assert ab.encode() != ba.encode()


# ----- Url serialisation: furl must not leak into the query bytes ----------


def test_url_str_preserves_pct20_not_plus() -> None:
    # Regression: furl re-encodes %20 -> + when it serialises a query. The
    # wire-exact query must be rendered by the pure codec and spliced verbatim.
    url = Url(scheme="https", host="h.example", path="/p", query=QueryParams([("q", "a b")]))
    assert str(url) == "https://h.example/p?q=a%20b"
    assert "+" not in str(url)


def test_url_str_places_query_before_fragment() -> None:
    url = Url(
        scheme="https",
        host="h.example",
        path="/p",
        query=QueryParams([("q", "a b")]),
        fragment="sec",
    )
    assert str(url) == "https://h.example/p?q=a%20b#sec"


def test_url_parse_keeps_literal_plus_and_decodes_pct20() -> None:
    url = Url.parse("https://h.example/p?q=a%20b&r=a+b&flag")
    assert url.query.get("q") == "a b"
    assert url.query.get("r") == "a+b"
    assert url.query.get("flag") == ""


def test_url_str_parse_round_trip_preserves_encoded_query() -> None:
    original = "https://h.example/p?q=a%20b&r=a%2Bb"
    assert str(Url.parse(original)) == original


# ----- IPv6 + userinfo -----------------------------------------------------


def test_ipv6_brackets_and_userinfo_survive_serialisation() -> None:
    url = Url.parse("https://user:pass@[::1]:8080/p?q=a%20b")
    assert url.host == "[::1]"
    assert url.port == 8080
    assert url.userinfo == "user:pass"
    # str() redacts userinfo but keeps the wire-exact query.
    assert str(url) == "https://[::1]:8080/p?q=a%20b"
    # wire_form() carries credentials and the same wire-exact query.
    assert url.wire_form() == "https://user:pass@[::1]:8080/p?q=a%20b"


# ----- textual (DNS-free) URL equality -------------------------------------


def test_url_equality_is_textual_not_dns_resolved() -> None:
    # Structural field equality: no host resolution is performed. A trailing-dot
    # FQDN and a differing port are textually distinct even if they would
    # resolve identically.
    assert Url.parse("https://example.com/") == Url.parse("https://example.com/")
    assert Url.parse("https://example.com./") != Url.parse("https://example.com/")
    assert Url.parse("https://example.com:8443/") != Url.parse("https://example.com/")


# ----- property: encode/parse inversion over arbitrary UTF-8 pairs ---------

_component = st.text(st.characters(codec="utf-8"), max_size=24)
_pairs = st.lists(st.tuples(_component, _component), max_size=12)


@given(pairs=_pairs)
def test_render_parse_is_exact_inverse(pairs: list[tuple[str, str]]) -> None:
    """Parsing a rendered query reproduces the exact ordered pairs."""
    assert parse_query(render_query(pairs)) == pairs
