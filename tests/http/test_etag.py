"""Tests for ``ETag`` parsing and comparison."""
from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import ETag


def test_parse_strong() -> None:
    tag = ETag.parse('"abc123"')
    assert tag.value == "abc123"
    assert tag.weak is False


def test_parse_weak() -> None:
    tag = ETag.parse('W/"abc"')
    assert tag.value == "abc"
    assert tag.weak is True


def test_str_round_trip_strong() -> None:
    tag = ETag(value="abc")
    assert str(tag) == '"abc"'
    assert ETag.parse(str(tag)) == tag


def test_str_round_trip_weak() -> None:
    tag = ETag(value="abc", weak=True)
    assert str(tag) == 'W/"abc"'
    assert ETag.parse(str(tag)) == tag


def test_parse_unquoted_raises() -> None:
    with pytest.raises(ValueError):
        ETag.parse("no-quotes")


def test_strong_comparison() -> None:
    a = ETag(value="x")
    b = ETag(value="x")
    weak = ETag(value="x", weak=True)
    assert a.matches_strong(b)
    # Either side being weak disables strong-match per RFC 7232 §2.3.2.
    assert not a.matches_strong(weak)
    assert not weak.matches_strong(weak)


def test_weak_comparison() -> None:
    a = ETag(value="x")
    weak = ETag(value="x", weak=True)
    assert a.matches_weak(weak)
    assert weak.matches_weak(weak)
    different = ETag(value="y")
    assert not a.matches_weak(different)
