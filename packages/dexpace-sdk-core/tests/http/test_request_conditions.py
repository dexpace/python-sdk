# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``RequestConditions.apply_to``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from dexpace.sdk.core.http.common import ETag, RequestConditions, Url
from dexpace.sdk.core.http.request import Method, Request


def _request() -> Request:
    return Request(method=Method.GET, url=Url.parse("https://example.com/"))


def test_if_match_single_etag() -> None:
    cond = RequestConditions(if_match=[ETag(value="abc")])
    result = cond.apply_to(_request())
    assert result.headers.get("if-match") == '"abc"'


def test_if_none_match_multiple() -> None:
    cond = RequestConditions(if_none_match=[ETag(value="a"), ETag(value="b", weak=True)])
    result = cond.apply_to(_request())
    assert result.headers.get("if-none-match") == '"a", W/"b"'


def test_wildcard_if_match() -> None:
    cond = RequestConditions(if_match=[ETag(value="*")])
    result = cond.apply_to(_request())
    # Wildcard is emitted without quotes per RFC 7232 §3.1.
    assert result.headers.get("if-match") == "*"


def test_if_modified_since() -> None:
    when = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    cond = RequestConditions(if_modified_since=when)
    result = cond.apply_to(_request())
    value = result.headers.get("if-modified-since")
    assert value is not None
    assert "01 Jan 2024" in value
    assert "GMT" in value


def test_naive_datetime_treated_as_utc() -> None:
    naive = datetime(2024, 1, 1, 12, 0, 0)
    cond = RequestConditions(if_unmodified_since=naive)
    result = cond.apply_to(_request())
    value = result.headers.get("if-unmodified-since")
    assert value is not None and "GMT" in value


def test_non_utc_offset_normalized_to_gmt() -> None:
    # An aware datetime with a non-UTC offset must be normalized to UTC before
    # formatting; format_datetime(usegmt=True) rejects any other offset outright.
    plus_five = timezone(timedelta(hours=5))
    when = datetime(2024, 1, 1, 17, 0, 0, tzinfo=plus_five)
    cond = RequestConditions(if_modified_since=when)
    result = cond.apply_to(_request())
    value = result.headers.get("if-modified-since")
    # 17:00 at +05:00 is 12:00 UTC.
    assert value == "Mon, 01 Jan 2024 12:00:00 GMT"


def test_apply_to_returns_new_instance() -> None:
    request = _request()
    cond = RequestConditions(if_match=[ETag(value="x")])
    result = cond.apply_to(request)
    assert result is not request
    assert "if-match" not in request.headers


# --- Conformance battery (HTTP-50; RFC 7232 §3) -----------------------------


class TestETagListRendering:
    def test_if_match_multiple_comma_joined(self) -> None:
        cond = RequestConditions(if_match=[ETag(value="a"), ETag(value="b")])
        assert cond.apply_to(_request()).headers.get("if-match") == '"a", "b"'

    def test_if_match_mixes_strong_and_weak(self) -> None:
        cond = RequestConditions(if_match=[ETag(value="a"), ETag(value="b", weak=True)])
        assert cond.apply_to(_request()).headers.get("if-match") == '"a", W/"b"'

    def test_if_none_match_wildcard(self) -> None:
        cond = RequestConditions(if_none_match=[ETag(value="*")])
        # RFC 7232 §3.2: the wildcard is emitted bare, never quoted.
        assert cond.apply_to(_request()).headers.get("if-none-match") == "*"

    def test_wildcard_only_special_cased_when_single_strong(self) -> None:
        # A ``*`` alongside another tag is an ordinary (quoted) entity-tag.
        cond = RequestConditions(if_match=[ETag(value="*"), ETag(value="a")])
        assert cond.apply_to(_request()).headers.get("if-match") == '"*", "a"'

    def test_empty_etag_list_raises(self) -> None:
        cond = RequestConditions(if_match=[])
        with pytest.raises(ValueError):
            cond.apply_to(_request())


class TestHeaderSelection:
    def test_no_conditions_adds_no_headers(self) -> None:
        result = RequestConditions().apply_to(_request())
        for name in ("if-match", "if-none-match", "if-modified-since", "if-unmodified-since"):
            assert name not in result.headers

    def test_all_four_conditions_render_all_headers(self) -> None:
        when = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        cond = RequestConditions(
            if_match=[ETag(value="a")],
            if_none_match=[ETag(value="b")],
            if_modified_since=when,
            if_unmodified_since=when,
        )
        headers = cond.apply_to(_request()).headers
        assert headers.get("if-match") == '"a"'
        assert headers.get("if-none-match") == '"b"'
        assert headers.get("if-modified-since") == "Mon, 01 Jan 2024 12:00:00 GMT"
        assert headers.get("if-unmodified-since") == "Mon, 01 Jan 2024 12:00:00 GMT"

    def test_apply_to_replaces_existing_conditional_header(self) -> None:
        base = _request().with_header("if-match", '"stale"')
        result = RequestConditions(if_match=[ETag(value="fresh")]).apply_to(base)
        assert result.headers.get("if-match") == '"fresh"'


class TestHttpDateFormatting:
    def test_utc_datetime_rendered_as_imf_fixdate(self) -> None:
        when = datetime(2024, 3, 5, 8, 9, 10, tzinfo=UTC)
        cond = RequestConditions(if_modified_since=when)
        assert cond.apply_to(_request()).headers.get("if-modified-since") == (
            "Tue, 05 Mar 2024 08:09:10 GMT"
        )

    def test_non_utc_offset_normalized(self) -> None:
        plus_five = timezone(timedelta(hours=5))
        when = datetime(2024, 1, 1, 17, 0, 0, tzinfo=plus_five)
        cond = RequestConditions(if_unmodified_since=when)
        assert cond.apply_to(_request()).headers.get("if-unmodified-since") == (
            "Mon, 01 Jan 2024 12:00:00 GMT"
        )

    def test_naive_datetime_treated_as_utc(self) -> None:
        naive = datetime(2024, 1, 1, 12, 0, 0)
        cond = RequestConditions(if_modified_since=naive)
        assert cond.apply_to(_request()).headers.get("if-modified-since") == (
            "Mon, 01 Jan 2024 12:00:00 GMT"
        )
