# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Conformance battery for ``MediaType`` (HTTP-23..27, HTTP-53).

Certifies the RFC 7231/7230 behaviours the SDK relies on: the normalisation
invariant lives behind unexported fields read through accessors (HTTP-53); the
quoted-string-aware parse/render loop round-trips (HTTP-23/24); type, subtype,
and parameter keys lower-case while parameter values are preserved verbatim
(HTTP-25/26); and the ``charset`` accessor degrades an unknown encoding to
``None`` (HTTP-27).

`hypothesis` is not a workspace dev dependency (see the root ``pyproject.toml``
``[dependency-groups]``), so the parse/render round-trip is exercised as an
enumerated property over a curated corpus of separator-, quote-, escape-, and
non-ASCII-bearing values rather than as a generated property test.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from dexpace.sdk.core.http.common import MediaType


class TestInvariantBehindAccessor:
    """HTTP-53: the type/subtype/params invariant cannot be bypassed.

    ``__post_init__`` normalises on every construction path — including the
    raw dataclass constructor, not just ``of``/``parse`` — so an
    un-normalised instance can never be fabricated, and the frozen fields
    reject any later reassignment.

    Note: the normalised parts are plain frozen fields rather than private
    fields behind read-only properties, because CPython's
    ``@dataclass(frozen=True, slots=True)`` generates a broken
    ``__setattr__``/``__delattr__`` pair when the class defines a descriptor
    beyond its declared slotted fields (reproducible independent of this
    codebase, unrelated to the field's specific name) — see the class
    docstring in ``media_type.py``.
    """

    def test_fields_are_the_declared_slots(self) -> None:
        assert MediaType.__slots__ == ("type", "subtype", "parameters")

    def test_raw_constructor_normalises_type_and_subtype(self) -> None:
        # Bypassing ``of``/``parse`` still yields a normalised instance.
        mt = MediaType("APPLICATION", "JSON")
        assert mt.type == "application"
        assert mt.subtype == "json"

    def test_raw_constructor_lowercases_keys_and_sorts_params(self) -> None:
        mt = MediaType("text", "plain", (("B", "2"), ("A", "1")))
        assert mt.parameters == (("a", "1"), ("b", "2"))

    def test_raw_constructor_preserves_parameter_value_case(self) -> None:
        mt = MediaType("multipart", "form-data", (("Boundary", "AbC"),))
        assert mt.parameters == (("boundary", "AbC"),)

    def test_assigning_field_raises_frozen(self) -> None:
        # ``setattr`` via a name variable keeps this a runtime-immutability
        # assertion without tripping a static frozen-field check.
        mt = MediaType.of("application", "json")
        field = "type"
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(mt, field, "text")

    def test_raw_and_factory_construction_are_equal_and_hash_equal(self) -> None:
        raw = MediaType("Application", "Json", (("B", "2"), ("A", "1")))
        made = MediaType.of("application", "json", {"a": "1", "b": "2"})
        assert raw == made
        assert hash(raw) == hash(made)


class TestParameterAccessorReturnsSortedPairs:
    def test_parameters_sorted_by_key(self) -> None:
        mt = MediaType.of("text", "plain", {"z": "1", "a": "2", "m": "3"})
        keys = [k for k, _ in mt.parameters]
        assert keys == sorted(keys)

    def test_parameters_is_tuple_not_mutable(self) -> None:
        mt = MediaType.of("text", "plain", {"a": "1"})
        assert isinstance(mt.parameters, tuple)


class TestCharsetAccessor:
    def test_known_charset_returned(self) -> None:
        assert MediaType.parse("text/plain; charset=utf-8").charset == "utf-8"

    def test_charset_case_preserved(self) -> None:
        assert MediaType.parse("text/plain; charset=UTF-8").charset == "UTF-8"

    def test_absent_charset_is_none(self) -> None:
        assert MediaType.of("application", "json").charset is None

    def test_unknown_charset_degrades_to_none(self) -> None:
        mt = MediaType.parse("text/plain; charset=definitely-not-a-codec")
        assert dict(mt.parameters)["charset"] == "definitely-not-a-codec"
        assert mt.charset is None

    def test_empty_charset_degrades_to_none(self) -> None:
        # A quoted empty charset parses to "" and must not raise via codecs.
        mt = MediaType.parse('text/plain; charset=""')
        assert mt.charset is None


class TestFullTypeAndWildcards:
    def test_full_type_excludes_parameters(self) -> None:
        mt = MediaType.of("text", "plain", {"charset": "utf-8"})
        assert mt.full_type == "text/plain"

    def test_full_type_for_wildcards(self) -> None:
        assert MediaType.of("*", "*").full_type == "*/*"

    def test_includes_is_reflexive(self) -> None:
        mt = MediaType.of("application", "json")
        assert mt.includes(mt)

    def test_includes_ignores_parameters(self) -> None:
        a = MediaType.of("text", "plain", {"charset": "utf-8"})
        b = MediaType.of("text", "plain", {"charset": "ascii"})
        assert a.includes(b)

    def test_concrete_type_wildcard_subtype_allowed(self) -> None:
        pattern = MediaType.of("application", "*")
        assert pattern.includes(MediaType.of("application", "json"))


class TestQuotedStringRendering:
    """HTTP-24: values needing quoting are quoted and escaped; tokens are bare."""

    def test_bare_token_value_not_quoted(self) -> None:
        assert str(MediaType.of("text", "plain", {"charset": "utf-8"})) == (
            "text/plain;charset=utf-8"
        )

    @pytest.mark.parametrize(
        "value",
        ["foo bar", "a;b", "a,b", "a/b", "a=b c", "a\tb", "café", "π", 'has"quote', "back\\slash"],
    )
    def test_values_needing_quotes_round_trip(self, value: str) -> None:
        mt = MediaType.of("multipart", "mixed", {"p": value})
        rendered = str(mt)
        # Non-token values must be wrapped in a quoted-string ...
        assert rendered.startswith('multipart/mixed;p="')
        # ... and survive a parse/render round-trip byte-for-byte.
        assert MediaType.parse(rendered) == mt
        assert dict(MediaType.parse(rendered).parameters)["p"] == value

    def test_empty_value_renders_as_empty_quoted_string(self) -> None:
        mt = MediaType.of("text", "plain", {"p": ""})
        assert str(mt) == 'text/plain;p=""'
        assert MediaType.parse(str(mt)) == mt


def _corpus() -> list[str]:
    """Curated parameter-value fragments for the enumerated round-trip property."""
    fragments = [
        "token",
        "UPPER",
        "MiXeD-Case_1.0",
        "utf-8",
        "abc=def",
        "YWJj==",
        "foo bar",
        "a;b;c",
        "a,b",
        'has"dquote',
        "back\\slash",
        "semi;and space",
        "café-π",
        "",
        " leading-token",
    ]
    return fragments


class TestParseRenderRoundTripProperty:
    """HTTP-23: enumerated property — ``parse(str(mt)) == mt`` and str is a fixed point."""

    @pytest.mark.parametrize("value", _corpus())
    def test_single_parameter_round_trip(self, value: str) -> None:
        mt = MediaType.of("multipart", "mixed", {"boundary": value})
        rendered = str(mt)
        reparsed = MediaType.parse(rendered)
        assert reparsed == mt
        # ``str`` is a fixed point of parse: the canonical form re-renders to itself.
        assert str(reparsed) == rendered

    @pytest.mark.parametrize(
        "first,second",
        list(itertools.islice(itertools.product(_corpus(), repeat=2), 0, None, 3)),
    )
    def test_two_parameter_round_trip(self, first: str, second: str) -> None:
        mt = MediaType.of("multipart", "mixed", {"boundary": first, "tag": second})
        rendered = str(mt)
        reparsed = MediaType.parse(rendered)
        assert reparsed == mt
        assert str(reparsed) == rendered

    def test_type_subtype_case_is_a_fixed_point(self) -> None:
        for text in ("Application/JSON", "TEXT/Plain; Charset=UTF-8"):
            mt = MediaType.parse(text)
            assert MediaType.parse(str(mt)) == mt


class TestParseEdgeCases:
    def test_duplicate_parameter_last_wins(self) -> None:
        mt = MediaType.parse("text/plain; charset=utf-8; charset=ascii")
        assert dict(mt.parameters)["charset"] == "ascii"
        assert len(mt.parameters) == 1

    def test_trailing_semicolon_ignored(self) -> None:
        mt = MediaType.parse("application/json;")
        assert mt.full_type == "application/json"
        assert mt.parameters == ()

    def test_whitespace_around_equals_tolerated(self) -> None:
        mt = MediaType.parse("text/plain; charset = utf-8")
        assert mt.charset == "utf-8"

    def test_empty_parameter_value_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.parse("text/plain; charset=")

    def test_empty_parameter_key_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.parse("text/plain; =utf-8")

    def test_parameter_without_equals_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.parse("text/plain; charset")

    def test_leading_slash_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.parse("/json")

    def test_wildcard_type_concrete_subtype_raises(self) -> None:
        with pytest.raises(ValueError):
            MediaType.of("*", "json")
