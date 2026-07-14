# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Conformance tests for the verbatim raw-query pagination splice.

The ``pagination_splice`` (``PAGE-21``) family of the golden wire-exactness
corpus is driven directly through the production splice (``splice_query``). The
splice is a byte-for-byte token-level rewrite: every untouched query parameter
survives verbatim (a literal ``+`` is neither re-encoded to ``%2B`` nor decoded
to a space, a pre-encoded ``%20`` is preserved), so the corpus proves the splice
never performs a lossy ``parse_qs`` / ``urlencode`` round-trip.

Non-query URL components (scheme, host, path, fragment) are peeled off with
``urlsplit`` / ``urlunsplit`` — raw-component operations that never decode — so
only the query string reaches the splice, exactly as a strategy rewrites the
query of an otherwise-untouched request URL.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import pytest

from dexpace.sdk.core.http.common._query_codec import remove_query_param, splice_query
from dexpace.sdk.tck.golden_corpus import PaginationSpliceVector, load_pagination_splice

_VECTORS = load_pagination_splice()


def _splice_url(base: str, name: str, value: str) -> str:
    """Splice ``name=value`` into ``base``'s query, preserving other components."""
    parts = urlsplit(base)
    new_query = splice_query(parts.query, name, value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


# ----- golden corpus: PAGE-21 byte-for-byte -------------------------------


@pytest.mark.req("PAGE-21", "PAGE-24")
@pytest.mark.parametrize("vector", _VECTORS, ids=lambda v: v.id)
def test_splice_matches_golden_byte_for_byte(vector: PaginationSpliceVector) -> None:
    """The production splice reproduces every corpus vector byte for byte."""
    spliced = _splice_url(
        vector.base.decode("ascii"),
        vector.param.decode("ascii"),
        vector.value.decode("ascii"),
    )
    assert spliced == vector.spliced.decode("ascii"), vector.id


@pytest.mark.req("PAGE-21")
@pytest.mark.parametrize("vector", _VECTORS, ids=lambda v: v.id)
def test_splice_preserves_every_untouched_param_verbatim(
    vector: PaginationSpliceVector,
) -> None:
    """Each raw ``name=value`` the vector pins as untouched survives verbatim."""
    spliced = _splice_url(
        vector.base.decode("ascii"),
        vector.param.decode("ascii"),
        vector.value.decode("ascii"),
    )
    for untouched in vector.untouched:
        assert untouched.decode("ascii") in spliced, (vector.id, untouched)


# ----- splice_query unit semantics ----------------------------------------


def test_splice_appends_to_empty_query() -> None:
    assert splice_query("", "cursor", "c1") == "cursor=c1"


def test_splice_appends_absent_key_with_ampersand() -> None:
    assert splice_query("limit=50", "cursor", "c1") == "limit=50&cursor=c1"


@pytest.mark.req("PAGE-21")
def test_splice_replaces_first_occurrence_in_place() -> None:
    # Position is preserved: the replaced key keeps its slot; trailing params
    # do not shift, and a second occurrence is left untouched by a "set".
    assert splice_query("cursor=c1&limit=50", "cursor", "c2") == "cursor=c2&limit=50"
    assert splice_query("a=1&cursor=old&b=2&cursor=stale", "cursor", "new") == (
        "a=1&cursor=new&b=2&cursor=stale"
    )


@pytest.mark.req("PAGE-21", "PAGE-22")
def test_splice_leaves_a_literal_plus_untouched() -> None:
    # A literal '+' in an untouched param is neither re-encoded to %2B nor
    # decoded to a space — the token is copied through byte for byte.
    assert splice_query("tag=a+b", "cursor", "c2") == "tag=a+b&cursor=c2"


def test_splice_leaves_pre_encoded_bytes_untouched() -> None:
    assert splice_query("q=a%20b&sort=desc", "cursor", "c2") == "q=a%20b&sort=desc&cursor=c2"


@pytest.mark.req("PAGE-22")
def test_splice_encodes_only_the_new_pair() -> None:
    # The freshly-spliced value is strictly RFC 3986 encoded (space -> %20),
    # while the untouched neighbour keeps its own raw bytes.
    assert splice_query("tag=a+b", "q", "x y") == "tag=a+b&q=x%20y"


def test_splice_preserves_repeated_untouched_keys() -> None:
    assert splice_query("tag=x&tag=y", "cursor", "c2") == "tag=x&tag=y&cursor=c2"


# ----- remove_query_param unit semantics ----------------------------------


def test_remove_drops_every_occurrence() -> None:
    assert remove_query_param("a=1&cursor=c1&b=2&cursor=c2", "cursor") == "a=1&b=2"


def test_remove_absent_key_is_a_noop() -> None:
    assert remove_query_param("a=1&b=2", "cursor") == "a=1&b=2"


def test_remove_on_empty_query_is_empty() -> None:
    assert remove_query_param("", "cursor") == ""


def test_remove_preserves_untouched_bytes_verbatim() -> None:
    assert remove_query_param("q=a%20b&drop=x&tag=a+b", "drop") == "q=a%20b&tag=a+b"
