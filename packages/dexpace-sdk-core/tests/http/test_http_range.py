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


# --- Conformance battery (HTTP-49; RFC 7233 §2.1) ---------------------------


@pytest.mark.req("HTTP-49")
class TestFormatShapes:
    """``format`` renders the value portion; ``to_header_value`` adds ``bytes=``."""

    def test_bounded_format_omits_prefix(self) -> None:
        assert HttpRange(0, 100).format() == "0-99"

    def test_open_ended_format(self) -> None:
        assert HttpRange(50).format() == "50-"

    def test_suffix_format(self) -> None:
        assert HttpRange.suffix(20).format() == "-20"

    def test_single_byte_range(self) -> None:
        r = HttpRange(5, 1)
        assert r.end == 5
        assert r.to_header_value() == "bytes=5-5"

    def test_bounded_from_nonzero_start(self) -> None:
        assert HttpRange(200, 100).to_header_value() == "bytes=200-299"


class TestEndProperty:
    def test_end_of_bounded(self) -> None:
        assert HttpRange(0, 100).end == 99

    def test_end_of_open_ended_is_none(self) -> None:
        assert HttpRange(50).end is None

    def test_end_of_suffix_is_none(self) -> None:
        assert HttpRange.suffix(20).end is None


class TestConstructionValidation:
    def test_suffix_constructed_directly(self) -> None:
        r = HttpRange(count=5, is_suffix=True)
        assert r.format() == "-5"

    def test_suffix_without_count_raises(self) -> None:
        with pytest.raises(ValueError):
            HttpRange(is_suffix=True)

    def test_negative_count_raises(self) -> None:
        with pytest.raises(ValueError):
            HttpRange(0, -3)

    def test_suffix_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            HttpRange.suffix(-5)


class TestFormatMany:
    def test_single_range(self) -> None:
        assert HttpRange.format_many([HttpRange(0, 100)]) == "bytes=0-99"

    def test_mixed_bounded_open_and_suffix(self) -> None:
        ranges = [HttpRange(0, 100), HttpRange(500), HttpRange.suffix(50)]
        assert HttpRange.format_many(ranges) == "bytes=0-99,500-,-50"
