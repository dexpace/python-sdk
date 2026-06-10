# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for ``HttpRange`` byte-range serialization."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import HttpRange


def test_bounded_range() -> None:
    r = HttpRange(0, 100)
    assert r.end == 99
    assert r.to_header_value() == "bytes=0-99"


def test_open_ended_range() -> None:
    r = HttpRange(50)
    assert r.end is None
    assert r.to_header_value() == "bytes=50-"


def test_suffix_range() -> None:
    s = HttpRange.suffix(20)
    assert s.to_header_value() == "bytes=-20"


def test_suffix_range_is_public_httprange() -> None:
    s = HttpRange.suffix(20)
    assert isinstance(s, HttpRange)
    assert s.is_suffix is True
    assert s.format() == "-20"


def test_format_many_with_suffix_range() -> None:
    # A suffix range is a single public HttpRange, so format_many accepts it.
    ranges: list[HttpRange] = [HttpRange(0, 100), HttpRange.suffix(50)]
    assert HttpRange.format_many(ranges) == "bytes=0-99,-50"


def test_negative_start_raises() -> None:
    with pytest.raises(ValueError):
        HttpRange(-1, 100)


def test_zero_count_raises() -> None:
    with pytest.raises(ValueError):
        HttpRange(0, 0)


def test_suffix_zero_raises() -> None:
    with pytest.raises(ValueError):
        HttpRange.suffix(0)


def test_multi_range_header() -> None:
    ranges = (HttpRange(0, 100), HttpRange(200, 100))
    assert HttpRange.format_many(ranges) == "bytes=0-99,200-299"


def test_format_many_empty_raises() -> None:
    with pytest.raises(ValueError):
        HttpRange.format_many(())
