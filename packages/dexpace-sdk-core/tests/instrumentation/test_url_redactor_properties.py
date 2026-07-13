# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Property-based tests pinning ``UrlRedactor`` totality on arbitrary input.

Invariant covered:

- ``UrlRedactor.redact`` never raises and always returns a ``str`` for any
  input string, under any redaction configuration. The redactor sits on the
  logging path and must fail closed on malformed input (returning the
  ``REDACTED:unparseable`` sentinel) rather than letting an exotic parse error
  escape into the log sink.

The example count and deadline are governed by the capped "ci" Hypothesis
profile registered in ``tests/conftest.py``.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dexpace.sdk.core.instrumentation import UrlRedactor

# Arbitrary text, plus URL-shaped strings assembled from parts, to stress the
# ``furl`` parse path (schemes, authority, userinfo, ports, query, fragment).
_text = st.text(st.characters(codec="utf-8"), max_size=40)
_url_shaped = st.builds(
    "{}://{}@{}:{}/{}?{}#{}".format,
    _text,
    _text,
    _text,
    _text,
    _text,
    _text,
    _text,
)
_any_input = st.one_of(_text, _url_shaped)


@given(raw=_any_input)
def test_redact_never_raises_on_arbitrary_strings(raw: str) -> None:
    """Redacting any string returns a ``str`` and never raises."""
    result = UrlRedactor().redact(raw)
    assert isinstance(result, str)


@given(
    raw=_any_input,
    redact_path=st.booleans(),
    redact_fragment=st.booleans(),
    allowlist=st.frozensets(st.text(max_size=8), max_size=5),
)
def test_redact_totality_across_configs(
    raw: str,
    redact_path: bool,
    redact_fragment: bool,
    allowlist: frozenset[str],
) -> None:
    """Redaction stays total under any path/fragment/allowlist configuration."""
    redactor = UrlRedactor(
        allowlist,
        redact_path=redact_path,
        redact_fragment=redact_fragment,
    )
    assert isinstance(redactor.redact(raw), str)
