# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

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


def test_parse_embedded_quote_raises() -> None:
    # RFC 7232 forbids a DQUOTE inside the opaque tag; accepting it would
    # re-emit a malformed wire form via __str__.
    with pytest.raises(ValueError):
        ETag.parse('"a"b"')


def test_parse_embedded_control_char_raises() -> None:
    with pytest.raises(ValueError):
        ETag.parse('"a\x01b"')


def test_parse_embedded_space_raises() -> None:
    # RFC 7232 §2.3: etagc = %x21 / %x23-7E / obs-text, so SP (0x20) is not a
    # valid entity-tag character.
    with pytest.raises(ValueError):
        ETag.parse('"a b"')


def test_parse_empty_strong_etag_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        ETag.parse('""')


def test_parse_empty_weak_etag_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        ETag.parse('W/""')


def test_parse_non_empty_round_trips() -> None:
    strong = ETag(value="abc123")
    weak = ETag(value="abc123", weak=True)
    assert ETag.parse(str(strong)) == strong
    assert ETag.parse(str(weak)) == weak


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


# --- Conformance battery (HTTP-48; RFC 7232 §2.3, §2.3.2) --------------------


class TestStrongComparison:
    """RFC 7232 §2.3.2: strong compare requires exact match and both strong."""

    def test_two_strong_equal_match(self) -> None:
        assert ETag(value="x").matches_strong(ETag(value="x"))

    def test_strong_differing_values_do_not_match(self) -> None:
        assert not ETag(value="x").matches_strong(ETag(value="y"))

    def test_weak_vs_strong_never_strong_matches_either_order(self) -> None:
        strong = ETag(value="x")
        weak = ETag(value="x", weak=True)
        assert not strong.matches_strong(weak)
        assert not weak.matches_strong(strong)

    def test_two_weak_do_not_strong_match_even_if_equal(self) -> None:
        assert not ETag(value="x", weak=True).matches_strong(ETag(value="x", weak=True))

    def test_strong_match_is_symmetric(self) -> None:
        a, b = ETag(value="x"), ETag(value="x")
        assert a.matches_strong(b) == b.matches_strong(a)


class TestWeakComparison:
    """RFC 7232 §2.3.2: weak compare ignores the weak flag on either side."""

    @pytest.mark.parametrize(
        "left,right",
        [
            (ETag(value="x"), ETag(value="x")),
            (ETag(value="x"), ETag(value="x", weak=True)),
            (ETag(value="x", weak=True), ETag(value="x")),
            (ETag(value="x", weak=True), ETag(value="x", weak=True)),
        ],
    )
    def test_equal_values_weak_match_regardless_of_flags(self, left: ETag, right: ETag) -> None:
        assert left.matches_weak(right)

    def test_differing_values_never_weak_match(self) -> None:
        assert not ETag(value="x").matches_weak(ETag(value="y", weak=True))


class TestEqualityDistinctFromComparison:
    """Dataclass ``==`` includes the weak flag; RFC comparison helpers do not."""

    def test_dataclass_equality_considers_weak_flag(self) -> None:
        assert ETag(value="x") == ETag(value="x")
        assert ETag(value="x") != ETag(value="x", weak=True)

    def test_weak_match_holds_where_dataclass_equality_does_not(self) -> None:
        strong = ETag(value="x")
        weak = ETag(value="x", weak=True)
        assert strong != weak  # different objects by dataclass rules ...
        assert strong.matches_weak(weak)  # ... yet weak-equivalent per RFC


class TestParseCharacterSet:
    """RFC 7232 §2.3: etagc = %x21 / %x23-7E / obs-text (%x80-FF)."""

    def test_obs_text_high_bytes_permitted(self) -> None:
        # 0x80 and 0xFF are obs-text and stay valid inside the opaque tag.
        tag = ETag.parse('"a\x80\xffb"')
        assert tag.value == "a\x80\xffb"

    def test_del_char_rejected(self) -> None:
        with pytest.raises(ValueError):
            ETag.parse('"a\x7fb"')

    def test_surrounding_whitespace_stripped(self) -> None:
        assert ETag.parse('  "abc"  ') == ETag(value="abc")

    def test_weak_indicator_is_case_sensitive(self) -> None:
        # RFC 7232 §2.3: the weak indicator is the exact bytes "W/"; a
        # lower-case "w/" is not a valid weak prefix.
        with pytest.raises(ValueError):
            ETag.parse('w/"abc"')

    def test_single_character_value_valid(self) -> None:
        assert ETag.parse('"a"').value == "a"
