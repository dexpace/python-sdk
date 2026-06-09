# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""F8 parameter-parsing edge cases for ``MediaType``.

Covers the four correctness-bearing details called out in the platform
analysis: splitting a parameter on the *first* ``=`` only, stripping and
unescaping quoted-strings per RFC 7230 §3.2.6, lower-casing type/subtype and
parameter *keys* while preserving parameter *values*, and degrading an unknown
charset to ``None`` instead of raising.
"""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import MediaType


class TestSplitOnFirstEquals:
    def test_boundary_with_multiple_equals_keeps_remainder(self) -> None:
        mt = MediaType.parse("multipart/form-data; boundary=abc=def")
        assert dict(mt.parameters)["boundary"] == "abc=def"

    def test_base64_padding_in_value_preserved(self) -> None:
        mt = MediaType.parse("application/octet-stream; tag=YWJj==")
        assert dict(mt.parameters)["tag"] == "YWJj=="

    def test_quoted_value_containing_equals(self) -> None:
        mt = MediaType.parse('multipart/mixed; boundary="a=b=c"')
        assert dict(mt.parameters)["boundary"] == "a=b=c"


class TestQuotedStringHandling:
    def test_strips_surrounding_quotes(self) -> None:
        mt = MediaType.parse('text/plain; charset="utf-8"')
        assert mt.charset == "utf-8"

    def test_unescapes_escaped_quote(self) -> None:
        # \" -> "
        mt = MediaType.parse('text/plain; foo="a\\"b"')
        assert dict(mt.parameters)["foo"] == 'a"b'

    def test_unescapes_escaped_backslash(self) -> None:
        # \\ -> \
        mt = MediaType.parse('text/plain; foo="a\\\\b"')
        assert dict(mt.parameters)["foo"] == "a\\b"

    def test_unescapes_mixed_quoted_pairs(self) -> None:
        mt = MediaType.parse('text/plain; foo="a\\\\b\\"c"')
        assert dict(mt.parameters)["foo"] == 'a\\b"c'

    def test_bare_value_left_unchanged(self) -> None:
        mt = MediaType.parse("text/plain; charset=utf-8")
        assert mt.charset == "utf-8"

    def test_quoted_value_with_separators_preserved(self) -> None:
        # A quoted-string may legitimately contain token separators such as
        # spaces and semicolons (the latter only because it is quoted).
        mt = MediaType.parse('multipart/form-data; boundary="foo bar"')
        assert dict(mt.parameters)["boundary"] == "foo bar"


class TestCaseFolding:
    def test_type_and_subtype_lowercased(self) -> None:
        mt = MediaType.parse("APPLICATION/JSON")
        assert mt.full_type == "application/json"

    def test_parameter_key_lowercased(self) -> None:
        mt = MediaType.parse("text/plain; CHARSET=utf-8")
        assert "charset" in dict(mt.parameters)

    def test_parameter_value_case_preserved(self) -> None:
        # Boundaries and base64 tags are case-sensitive — only the key folds.
        mt = MediaType.parse("multipart/form-data; Boundary=AbCdEf")
        assert dict(mt.parameters)["boundary"] == "AbCdEf"

    def test_charset_value_case_preserved(self) -> None:
        mt = MediaType.parse("text/plain; charset=UTF-8")
        assert mt.charset == "UTF-8"


class TestUnknownCharsetDegradesToNone:
    def test_unknown_charset_returns_none(self) -> None:
        mt = MediaType.parse("text/plain; charset=not-a-real-encoding")
        # The parameter is retained verbatim ...
        assert dict(mt.parameters)["charset"] == "not-a-real-encoding"
        # ... but the typed accessor degrades to None rather than raising.
        assert mt.charset is None

    def test_unknown_charset_does_not_raise_on_parse(self) -> None:
        # Parsing must succeed even with a nonsense charset.
        mt = MediaType.parse("text/plain; charset=utf-99")
        assert mt.full_type == "text/plain"
        assert mt.charset is None

    @pytest.mark.parametrize(
        "charset",
        ["utf-8", "UTF-8", "latin-1", "iso-8859-1", "ascii", "utf-16"],
        ids=["utf8", "utf8-upper", "latin1", "iso8859", "ascii", "utf16"],
    )
    def test_known_charsets_round_trip(self, charset: str) -> None:
        mt = MediaType.parse(f"text/plain; charset={charset}")
        assert mt.charset == charset

    def test_absent_charset_returns_none(self) -> None:
        mt = MediaType.parse("application/json")
        assert mt.charset is None
