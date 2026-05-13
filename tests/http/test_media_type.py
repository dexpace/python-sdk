"""Tests for ``MediaType`` parsing, equality, and includes/wildcards."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import MediaType


class TestParse:
    def test_basic(self) -> None:
        mt = MediaType.parse("application/json")
        assert mt.type == "application"
        assert mt.subtype == "json"
        assert mt.parameters == ()

    def test_with_charset(self) -> None:
        mt = MediaType.parse("text/plain; charset=utf-8")
        assert mt.charset == "utf-8"

    def test_multipart_boundary_with_equals(self) -> None:
        # Boundary values can themselves contain "=" — splitting on first "=" only.
        mt = MediaType.parse("multipart/form-data; boundary=abc=def")
        assert dict(mt.parameters)["boundary"] == "abc=def"

    def test_blank_value_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.parse("")

    def test_malformed_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.parse("not-a-media-type")

    def test_missing_subtype_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.parse("application/")

    def test_parse_unquotes_charset(self) -> None:
        mt = MediaType.parse('text/plain; charset="utf-8"')
        assert mt.charset == "utf-8"

    def test_parse_quoted_pair(self) -> None:
        # Quoted-pair: a backslash escapes the following character per
        # RFC 7230 §3.2.6. ``"a\"b"`` decodes to ``a"b``.
        mt = MediaType.parse('text/plain; foo="a\\"b"')
        assert dict(mt.parameters)["foo"] == 'a"b'


class TestNormalisation:
    def test_lower_cases_type_and_subtype(self) -> None:
        mt = MediaType.of("APPLICATION", "JSON")
        assert mt.full_type == "application/json"

    def test_parameter_keys_normalised(self) -> None:
        mt = MediaType.of("text", "plain", {"CHARSET": "UTF-8"})
        # Keys are case-folded; values preserve case (multipart boundaries
        # and similar parameters depend on case).
        assert dict(mt.parameters) == {"charset": "UTF-8"}
        assert mt.charset == "UTF-8"

    def test_blank_type_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.of("  ", "json")

    def test_wildcard_type_requires_wildcard_subtype(self) -> None:
        with pytest.raises(ValueError):
            MediaType.of("*", "json")


class TestIncludes:
    def test_exact_match(self) -> None:
        a = MediaType.of("application", "json")
        b = MediaType.of("application", "json")
        assert a.includes(b)

    def test_wildcard_subtype(self) -> None:
        a = MediaType.of("application", "*")
        b = MediaType.of("application", "json")
        assert a.includes(b)
        assert not b.includes(a)

    def test_wildcard_type_and_subtype(self) -> None:
        a = MediaType.of("*", "*")
        b = MediaType.of("text", "plain")
        assert a.includes(b)

    def test_no_match(self) -> None:
        a = MediaType.of("application", "json")
        b = MediaType.of("text", "plain")
        assert not a.includes(b)


class TestSerialisation:
    def test_str_without_parameters(self) -> None:
        assert str(MediaType.of("application", "json")) == "application/json"

    def test_str_with_parameters(self) -> None:
        text = str(MediaType.of("text", "plain", {"charset": "utf-8"}))
        assert "charset=utf-8" in text

    def test_str_quotes_boundary_with_spaces(self) -> None:
        mt = MediaType.of("multipart", "form-data", {"boundary": "foo bar"})
        rendered = str(mt)
        assert 'boundary="foo bar"' in rendered
        # Round-trip: parsing the rendered form recovers the original value.
        assert MediaType.parse(rendered) == mt

    def test_equality_independent_of_param_order(self) -> None:
        a = MediaType.of("text", "plain", {"a": "1", "b": "2"})
        b = MediaType.of("text", "plain", {"b": "2", "a": "1"})
        assert a == b
        assert hash(a) == hash(b)
