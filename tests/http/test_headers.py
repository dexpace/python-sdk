"""Tests for ``Headers`` immutability, case-insensitivity, and multi-value semantics."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import Headers, HttpHeaderName
from dexpace.sdk.core.http.common.http_header_name import AUTHORIZATION, CONTENT_TYPE


class TestCaseInsensitiveLookup:
    def test_get_matches_regardless_of_case(self) -> None:
        h = Headers([("Content-Type", "application/json")])
        assert h.get("content-type") == "application/json"
        assert h.get("CONTENT-TYPE") == "application/json"
        assert h.get("Content-Type") == "application/json"

    def test_get_with_http_header_name(self) -> None:
        h = Headers([("Content-Type", "application/json")])
        assert h.get(CONTENT_TYPE) == "application/json"

    def test_get_default_returned_when_absent(self) -> None:
        h = Headers()
        assert h.get("missing") is None
        assert h.get("missing", "fallback") == "fallback"

    def test_contains_with_str_and_http_header_name(self) -> None:
        h = Headers([("Content-Type", "application/json")])
        assert "content-type" in h
        assert CONTENT_TYPE in h
        assert AUTHORIZATION not in h
        assert 123 not in h  # non-string returns False, doesn't raise

    def test_getitem_raises_keyerror_when_absent(self) -> None:
        h = Headers()
        with pytest.raises(KeyError):
            _ = h["missing"]


class TestMultiValue:
    def test_with_added_appends_to_existing_list(self) -> None:
        h = Headers([("Set-Cookie", "a=1")])
        result = h.with_added("set-cookie", "b=2")
        assert result.values("set-cookie") == ("a=1", "b=2")

    def test_with_added_creates_new_entry_when_absent(self) -> None:
        h = Headers()
        result = h.with_added("X-New", "value")
        assert result.values("x-new") == ("value",)

    def test_with_set_replaces_existing_values(self) -> None:
        h = Headers([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
        result = h.with_set("set-cookie", "only=one")
        assert result.values("set-cookie") == ("only=one",)

    def test_with_set_multiple_values(self) -> None:
        h = Headers()
        result = h.with_set("Vary", "Accept", "Accept-Encoding")
        assert result.values("vary") == ("Accept", "Accept-Encoding")

    def test_get_returns_first_value(self) -> None:
        h = Headers([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
        assert h.get("set-cookie") == "a=1"


class TestImmutability:
    def test_with_added_returns_new_instance(self) -> None:
        original = Headers([("A", "1")])
        new = original.with_added("B", "2")
        assert original.values("b") == ()
        assert new.values("b") == ("2",)

    def test_without_returns_new_instance(self) -> None:
        original = Headers([("A", "1"), ("B", "2")])
        new = original.without("A")
        assert "A" in original
        assert "A" not in new

    def test_without_is_noop_when_absent(self) -> None:
        original = Headers([("A", "1")])
        result = original.without("missing")
        assert result is original

    def test_setattr_raises(self) -> None:
        h = Headers()
        with pytest.raises(AttributeError):
            h.something = "value"

    def test_with_merged_combines(self) -> None:
        a = Headers([("Vary", "Accept")])
        b = Headers([("Vary", "Accept-Encoding"), ("X-Other", "1")])
        result = a.with_merged(b)
        assert result.values("vary") == ("Accept", "Accept-Encoding")
        assert result.get("x-other") == "1"


class TestOrMerge:
    def test_or_merges_headers(self) -> None:
        a = Headers([("Vary", "Accept")])
        b = Headers([("Vary", "Accept-Encoding"), ("X-Other", "1")])
        result = a | b
        assert result.values("vary") == ("Accept", "Accept-Encoding")
        assert result.get("x-other") == "1"

    def test_or_preserves_self_values(self) -> None:
        a = Headers([("X-Order", "first"), ("X-Only-A", "a")])
        b = Headers([("X-Order", "second"), ("X-Only-B", "b")])
        result = a | b
        # self's values appear before other's
        assert result.values("x-order") == ("first", "second")
        assert result.get("x-only-a") == "a"
        assert result.get("x-only-b") == "b"
        # originals are untouched
        assert a.values("x-order") == ("first",)
        assert b.values("x-order") == ("second",)

    def test_or_with_non_headers_raises_or_NotImplemented(self) -> None:  # noqa: N802
        a = Headers([("X-Foo", "1")])
        with pytest.raises(TypeError):
            _ = a | "not-headers"  # type: ignore[operator]
        with pytest.raises(TypeError):
            _ = a | 42  # type: ignore[operator]


class TestEqualityAndHashing:
    def test_equality_is_case_insensitive(self) -> None:
        a = Headers([("Content-Type", "json")])
        b = Headers([("content-type", "json")])
        assert a == b
        assert hash(a) == hash(b)

    def test_equality_value_preserving(self) -> None:
        a = Headers([("A", "x")])
        b = Headers([("A", "y")])
        assert a != b


class TestConstruction:
    def test_from_mapping(self) -> None:
        h = Headers({"Accept": "text/plain"})
        assert h.get("accept") == "text/plain"

    def test_from_iterable_of_pairs(self) -> None:
        h = Headers([("a", "1"), ("a", "2")])
        assert h.values("a") == ("1", "2")

    def test_empty_singleton(self) -> None:
        e1 = Headers.empty()
        e2 = Headers.empty()
        assert e1 is e2
        assert len(e1) == 0

    def test_repr_round_trippable(self) -> None:
        h = Headers([("X-Trace-Id", "abc")])
        text = repr(h)
        assert text.startswith("Headers(")
        assert "x-trace-id" in text
        assert "abc" in text


class TestHttpHeaderName:
    def test_str_is_canonical(self) -> None:
        assert str(CONTENT_TYPE) == "Content-Type"

    def test_value_is_lower(self) -> None:
        assert CONTENT_TYPE.value == "content-type"

    def test_of_derives_lower(self) -> None:
        name = HttpHeaderName.of("X-Custom-Header")
        assert name.value == "x-custom-header"
        assert name.canonical_name == "X-Custom-Header"


class TestValidation:
    @pytest.mark.parametrize("name", ["X-Bad\r\n", " X-Bad ", "X Bad", ""])
    def test_invalid_header_name_rejected(self, name: str) -> None:
        with pytest.raises(ValueError, match="invalid header name"):
            Headers([(name, "v")])

    @pytest.mark.parametrize("value", ["v\r\nLoc: bad", "v\nbad", "v\0bad"])
    def test_invalid_header_value_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="invalid header value"):
            Headers([("X-Test", value)])


class TestRepr:
    @pytest.mark.parametrize(
        "name", ["authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"]
    )
    def test_sensitive_header_values_redacted(self, name: str) -> None:
        h = Headers([(name, "secret-token-value")])
        rendered = repr(h)
        assert "secret-token-value" not in rendered
        assert "REDACTED" in rendered

    def test_non_sensitive_headers_not_redacted(self) -> None:
        h = Headers([("Content-Type", "application/json")])
        rendered = repr(h)
        assert "application/json" in rendered
        assert "REDACTED" not in rendered

    def test_mixed_headers_redact_only_sensitive(self) -> None:
        h = Headers(
            [
                ("Content-Type", "application/json"),
                ("Authorization", "Bearer abc"),
            ]
        )
        rendered = repr(h)
        assert "application/json" in rendered
        assert "Bearer abc" not in rendered
        assert "REDACTED" in rendered
