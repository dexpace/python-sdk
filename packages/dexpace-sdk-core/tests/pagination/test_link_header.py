# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the RFC 5988 ``Link`` header parser."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.pagination.link_header import find_rel, parse_link_header


def test_parse_single_link_value_extracts_target_and_rel() -> None:
    header = '<https://api.example.com/items?page=2>; rel="next"'
    parsed = parse_link_header(header)
    assert parsed == (("https://api.example.com/items?page=2", {"rel": "next"}),)


def test_parse_multiple_link_values_in_order() -> None:
    header = (
        '<https://api.example.com/items?page=2>; rel="next", '
        '<https://api.example.com/items?page=9>; rel="last"'
    )
    parsed = parse_link_header(header)
    assert [target for target, _ in parsed] == [
        "https://api.example.com/items?page=2",
        "https://api.example.com/items?page=9",
    ]
    assert parsed[0][1]["rel"] == "next"
    assert parsed[1][1]["rel"] == "last"


def test_comma_inside_quoted_param_does_not_split_link_values() -> None:
    header = '<https://api.example.com/a>; rel="next"; title="one, two"'
    parsed = parse_link_header(header)
    assert len(parsed) == 1
    assert parsed[0][1]["title"] == "one, two"


def test_param_names_are_case_insensitive() -> None:
    header = '<https://api.example.com/a>; REL="next"'
    assert find_rel(header, "next") == "https://api.example.com/a"


@pytest.mark.parametrize(
    ("header", "rel", "expected"),
    [
        ('<https://x/1>; rel="next"', "next", "https://x/1"),
        ('<https://x/1>; rel="next"', "prev", None),
        ('<https://x/1>; rel="first next"', "next", "https://x/1"),
        ('<https://x/1>; rel="NEXT"', "next", "https://x/1"),
        ("", "next", None),
        ("   ", "next", None),
    ],
    ids=[
        "next-present",
        "prev-absent",
        "next-in-space-separated-set",
        "case-insensitive-rel-value",
        "empty-header",
        "whitespace-header",
    ],
)
def test_find_rel_returns_matching_target(header: str, rel: str, expected: str | None) -> None:
    assert find_rel(header, rel) == expected


def test_unquoted_param_value_is_accepted() -> None:
    header = "<https://x/1>; rel=next"
    assert find_rel(header, "next") == "https://x/1"


def test_escaped_quote_inside_quoted_value_is_unescaped() -> None:
    header = r'<https://x/1>; rel="next"; title="a \"quoted\" word"'
    parsed = parse_link_header(header)
    assert parsed[0][1]["title"] == 'a "quoted" word'


def test_malformed_segment_without_brackets_is_ignored() -> None:
    header = 'rel="next", <https://x/1>; rel="last"'
    parsed = parse_link_header(header)
    assert len(parsed) == 1
    assert parsed[0][0] == "https://x/1"
