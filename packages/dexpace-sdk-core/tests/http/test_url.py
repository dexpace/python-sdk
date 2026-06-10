# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``Url`` parsing/serialisation and ``QueryParams`` multi-value behaviour."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import QueryParams, Url


class TestUrl:
    def test_basic_parse(self) -> None:
        u = Url.parse("https://api.example.com/v1/items")
        assert u.scheme == "https"
        assert u.host == "api.example.com"
        assert u.path == "/v1/items"
        assert u.port is None

    def test_explicit_port(self) -> None:
        u = Url.parse("https://api.example.com:8443/")
        assert u.port == 8443

    def test_query_parsed(self) -> None:
        u = Url.parse("https://api.example.com/?foo=1&bar=baz&foo=2")
        assert u.query.values("foo") == ("1", "2")
        assert u.query.get("bar") == "baz"

    def test_fragment_parsed(self) -> None:
        u = Url.parse("https://api.example.com/path#section")
        assert u.fragment == "section"

    def test_userinfo_parsed(self) -> None:
        u = Url.parse("https://user:pass@api.example.com/")
        assert u.userinfo == "user:pass"

    def test_str_round_trip(self) -> None:
        original = "https://api.example.com:8443/v1?a=1&b=2"
        u = Url.parse(original)
        assert str(u) == original

    def test_empty_url_raises(self) -> None:
        with pytest.raises(ValueError):
            Url.parse("")

    def test_missing_scheme_raises(self) -> None:
        with pytest.raises(ValueError):
            Url.parse("//api.example.com/path")

    def test_authority_includes_port(self) -> None:
        u = Url.parse("https://api.example.com:8443/")
        assert u.authority(with_userinfo=False) == "api.example.com:8443"

    def test_authority_with_userinfo_includes_credentials(self) -> None:
        u = Url.parse("https://user:pass@api.example.com:8443/")
        assert u.authority(with_userinfo=True) == "user:pass@api.example.com:8443"
        assert u.authority(with_userinfo=False) == "api.example.com:8443"

    def test_url_str_omits_userinfo(self) -> None:
        u = Url(scheme="https", host="example.com", userinfo="user:secret")
        rendered = str(u)
        assert "secret" not in rendered
        assert "user" not in rendered

    def test_url_wire_form_includes_userinfo(self) -> None:
        u = Url(scheme="https", host="example.com", userinfo="user:secret")
        assert "user:secret@example.com" in u.wire_form()

    def test_url_repr_redacts_userinfo(self) -> None:
        u = Url(scheme="https", host="example.com", userinfo="user:secret")
        rendered = repr(u)
        assert "REDACTED" in rendered
        assert "secret" not in rendered

    def test_url_repr_omits_redaction_marker_when_no_userinfo(self) -> None:
        u = Url(scheme="https", host="example.com")
        assert "REDACTED" not in repr(u)

    def test_with_path(self) -> None:
        u = Url.parse("https://example.com/old")
        new = u.with_path("/new")
        assert new.path == "/new"
        assert u.path == "/old"


class TestQueryParams:
    def test_get_first_value(self) -> None:
        q = QueryParams([("a", "1"), ("a", "2"), ("b", "x")])
        assert q.get("a") == "1"
        assert q.get("b") == "x"

    def test_values_returns_all(self) -> None:
        q = QueryParams([("a", "1"), ("a", "2")])
        assert q.values("a") == ("1", "2")

    def test_with_added_appends(self) -> None:
        q = QueryParams([("a", "1")])
        result = q.with_added("a", "2")
        assert result.values("a") == ("1", "2")
        assert q.values("a") == ("1",)

    def test_with_set_replaces(self) -> None:
        q = QueryParams([("a", "1"), ("a", "2")])
        result = q.with_set("a", "only")
        assert result.values("a") == ("only",)

    def test_with_set_no_values_removes_param(self) -> None:
        # ``with_set`` with zero values must remove the parameter entirely
        # (mirroring ``Headers.with_set``), not leave a phantom empty entry
        # where ``name in params`` is True but ``get`` returns None.
        q = QueryParams([("a", "1"), ("b", "2")])
        result = q.with_set("a")
        assert "a" not in result
        assert result.get("a") is None
        assert result.values("a") == ()
        assert "b" in result
        assert result.get("b") == "2"

    def test_with_set_no_values_on_absent_param_is_noop(self) -> None:
        q = QueryParams([("a", "1")])
        result = q.with_set("missing")
        assert "missing" not in result
        assert result.get("a") == "1"

    def test_with_set_no_values_removes_all_occurrences(self) -> None:
        q = QueryParams([("a", "1"), ("a", "2"), ("b", "3")])
        result = q.with_set("a")
        assert "a" not in result
        assert result.values("a") == ()

    def test_without(self) -> None:
        q = QueryParams([("a", "1"), ("b", "2")])
        result = q.without("a")
        assert "a" not in result
        assert "b" in result

    def test_case_sensitive(self) -> None:
        q = QueryParams([("FOO", "1")])
        assert q.get("foo") is None
        assert q.get("FOO") == "1"

    def test_encode_preserves_multi_values(self) -> None:
        q = QueryParams([("a", "1"), ("a", "2")])
        text = q.encode()
        # Either order is fine as long as both pairs appear.
        assert "a=1" in text and "a=2" in text

    def test_parse_round_trips(self) -> None:
        q = QueryParams.parse("a=1&b=hello%20world")
        assert q.get("a") == "1"
        assert q.get("b") == "hello world"

    def test_parse_leading_question_mark(self) -> None:
        q = QueryParams.parse("?a=1")
        assert q.get("a") == "1"

    def test_immutable(self) -> None:
        q = QueryParams()
        with pytest.raises(AttributeError):
            q.something = "x"
