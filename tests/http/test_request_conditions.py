"""Tests for ``RequestConditions.apply_to``."""
from __future__ import annotations

from datetime import UTC, datetime

from dexpace.sdk.core.http.common import ETag, RequestConditions
from dexpace.sdk.core.http.request import Method, Request


def _request() -> Request:
    return Request(method=Method.GET, url="https://example.com/")


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


def test_apply_to_returns_new_instance() -> None:
    request = _request()
    cond = RequestConditions(if_match=[ETag(value="x")])
    result = cond.apply_to(request)
    assert result is not request
    assert "if-match" not in request.headers
