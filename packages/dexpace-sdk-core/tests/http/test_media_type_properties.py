# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Property-based tests pinning ``MediaType`` parse/render round-trips.

Invariants covered:

- ``MediaType.parse(str(mt)) == mt`` — rendering to wire form and parsing back
  is the identity, including parameter values that require quoting (spaces,
  separators, quotes, control bytes, and non-ASCII text).
- ``MediaType.of`` normalisation is idempotent — constructing from an already
  normalised media type's parts reproduces it.

The example count and deadline are governed by the capped "ci" Hypothesis
profile registered in ``tests/conftest.py``.
"""

from __future__ import annotations

from string import ascii_lowercase

from hypothesis import assume, given
from hypothesis import strategies as st

from dexpace.sdk.core.http.common import MediaType

# RFC 7230 ``token`` characters. Generated lower-case because ``of`` lower-cases
# the type, subtype, and parameter keys anyway; keeping keys lower-case also
# avoids case-collision between distinct mapping keys after normalisation.
_TOKEN_CHARS = "!#$%&'*+-.^_`|~0123456789" + ascii_lowercase

_tokens = st.text(alphabet=_TOKEN_CHARS, min_size=1, max_size=20)
_param_keys = st.text(alphabet=_TOKEN_CHARS, min_size=1, max_size=15)
# Parameter values are unrestricted text: the renderer quotes anything that is
# not a bare token and the parser unquotes it, so the round-trip must survive
# spaces, separators, quotes, control bytes, and non-ASCII characters.
_param_values = st.text(st.characters(codec="utf-8"), max_size=25)
_params = st.dictionaries(_param_keys, _param_values, max_size=5)


@st.composite
def _media_types(draw: st.DrawFn) -> MediaType:
    """Build an arbitrary valid ``MediaType`` via the ``of`` factory."""
    type_ = draw(_tokens)
    subtype = draw(_tokens)
    # ``of`` rejects a wildcard type paired with a concrete subtype.
    assume(not (type_ == "*" and subtype != "*"))
    parameters = draw(_params)
    return MediaType.of(type_, subtype, parameters)


@given(media_type=_media_types())
def test_parse_render_round_trip(media_type: MediaType) -> None:
    """Parsing the rendered wire form reproduces the original media type."""
    assert MediaType.parse(str(media_type)) == media_type


@given(media_type=_media_types())
def test_of_normalization_is_idempotent(media_type: MediaType) -> None:
    """Re-running ``of`` on an already-normalised media type's parts is a no-op."""
    rebuilt = MediaType.of(
        media_type.type,
        media_type.subtype,
        dict(media_type.parameters),
    )
    assert rebuilt == media_type
