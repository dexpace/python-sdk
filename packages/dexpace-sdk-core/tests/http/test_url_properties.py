# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Property-based tests pinning ``QueryParams`` / ``Url`` encode/parse inversion.

Invariants covered:

- ``QueryParams.parse(qp.encode()) == qp`` — percent-encoding to
  ``application/x-www-form-urlencoded`` and parsing back is the identity for
  arbitrary names and values (including empty, reserved, and non-ASCII text).
- ``QueryParams(qp.flatten()) == qp`` — regrouping the flattened pairs is
  idempotent.
- ``Url.parse(str(url)) == url`` for URLs assembled from unreserved-character
  components, so serialise/parse round-trips the structural URL fields.

The example count and deadline are governed by the capped "ci" Hypothesis
profile registered in ``tests/conftest.py``.
"""

from __future__ import annotations

from string import ascii_letters, ascii_lowercase, digits

from hypothesis import given
from hypothesis import strategies as st

from dexpace.sdk.core.http.common import QueryParams, Url

# Arbitrary query names/values: percent-encoding must invert regardless of
# reserved characters, whitespace, ``+``, ``&``, ``=`` or non-ASCII text.
_qp_text = st.text(st.characters(codec="utf-8"), max_size=20)
_qp_entries = st.lists(st.tuples(_qp_text, _qp_text), max_size=10)


@given(entries=_qp_entries)
def test_encode_parse_round_trip(entries: list[tuple[str, str]]) -> None:
    """Encoding then parsing reproduces the original query parameters."""
    params = QueryParams(entries)
    assert QueryParams.parse(params.encode()) == params


@given(entries=_qp_entries)
def test_flatten_reconstruction_is_identity(entries: list[tuple[str, str]]) -> None:
    """Rebuilding ``QueryParams`` from its flattened pairs is the identity."""
    params = QueryParams(entries)
    assert QueryParams(params.flatten()) == params


# Components restricted to RFC 3986 ``unreserved`` characters so no encoder
# ever rewrites them, isolating the round-trip from percent-encoding
# canonicalisation differences between the serialiser and ``furl``.
_UNRESERVED = ascii_letters + digits + "-._~"

_label = st.text(alphabet=ascii_lowercase + digits, min_size=1, max_size=8)
_host = st.lists(_label, min_size=1, max_size=3).map(".".join)
_segment = st.text(alphabet=_UNRESERVED, min_size=1, max_size=8).filter(
    lambda segment: segment not in (".", "..")
)
_path = st.lists(_segment, max_size=3).map(lambda segments: "".join("/" + s for s in segments))
_scheme = st.sampled_from(["http", "https"])
# Exclude the scheme default ports (80 / 443): serialisers legitimately omit
# them, which is a separate normalisation from the structural round-trip.
_port = st.none() | st.integers(min_value=1, max_value=65535).filter(lambda p: p not in (80, 443))
_url_query = st.lists(
    st.tuples(
        st.text(alphabet=_UNRESERVED, min_size=1, max_size=6),
        st.text(alphabet=_UNRESERVED, min_size=1, max_size=6),
    ),
    max_size=4,
)


@given(
    scheme=_scheme,
    host=_host,
    path=_path,
    port=_port,
    query=_url_query,
)
def test_url_str_parse_round_trip(
    scheme: str,
    host: str,
    path: str,
    port: int | None,
    query: list[tuple[str, str]],
) -> None:
    """Serialising a URL and parsing it back preserves the structural fields."""
    url = Url(scheme=scheme, host=host, path=path, port=port, query=QueryParams(query))
    parsed = Url.parse(str(url))
    assert parsed.scheme == url.scheme
    assert parsed.host == url.host
    assert parsed.port == url.port
    assert parsed.path == url.path
    assert parsed.query == url.query
