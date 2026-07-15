# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the shared RFC 1123 HTTP-date helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from dexpace.sdk.core.util import format_http_date, parse_http_date

# The canonical example from RFC 9110 / RFC 1123.
_CANONICAL = "Sun, 06 Nov 1994 08:49:37 GMT"
_CANONICAL_DT = datetime(1994, 11, 6, 8, 49, 37, tzinfo=UTC)


@pytest.mark.req("CFG-29")
def test_format_renders_canonical_rfc1123() -> None:
    assert format_http_date(_CANONICAL_DT) == _CANONICAL


def test_format_treats_naive_datetime_as_utc() -> None:
    naive = datetime(1994, 11, 6, 8, 49, 37)
    assert format_http_date(naive) == _CANONICAL


@pytest.mark.req("CFG-29")
def test_format_converts_aware_datetime_to_gmt() -> None:
    # 09:49:37 +0100 is 08:49:37 GMT.
    aware = datetime(1994, 11, 6, 9, 49, 37, tzinfo=timezone(timedelta(hours=1)))
    assert format_http_date(aware) == _CANONICAL


@pytest.mark.req("CFG-30")
def test_parse_rfc1123() -> None:
    assert parse_http_date(_CANONICAL) == _CANONICAL_DT


@pytest.mark.req("CFG-30")
@pytest.mark.parametrize(
    "raw",
    [
        "Sun, 06 Nov 1994 08:49:37 GMT",  # RFC 1123
        "Sunday, 06-Nov-94 08:49:37 GMT",  # RFC 850 (obsolete)
        "Sun Nov  6 08:49:37 1994",  # C asctime
    ],
)
def test_parse_accepts_all_three_http_date_formats(raw: str) -> None:
    parsed = parse_http_date(raw)
    assert parsed is not None
    # All three encode the same instant; compare in UTC.
    assert parsed.astimezone(UTC) == _CANONICAL_DT


def test_parse_attaches_utc_to_naive_forms() -> None:
    # The asctime form carries no zone; it must come back tz-aware (UTC).
    parsed = parse_http_date("Sun Nov  6 08:49:37 1994")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("raw", ["", "not-a-date", "Sun, 99 Xxx 9999", "garbage 123"])
def test_parse_returns_none_on_unparseable(raw: str) -> None:
    assert parse_http_date(raw) is None


def test_round_trip() -> None:
    rendered = format_http_date(_CANONICAL_DT)
    assert parse_http_date(rendered) == _CANONICAL_DT
