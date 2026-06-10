# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for header-value injection guards on mutators and hand-built names."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.http.common import Headers, HttpHeaderName

_BAD_VALUES = ["v\r\nLoc: bad", "v\nbad", "v\0bad"]


class TestMutatorValueValidation:
    @pytest.mark.parametrize("value", _BAD_VALUES)
    def test_with_added_rejects_control_chars(self, value: str) -> None:
        h = Headers()
        with pytest.raises(ValueError, match="invalid header value"):
            h.with_added("X-Test", value)

    @pytest.mark.parametrize("value", _BAD_VALUES)
    def test_with_added_rejects_control_chars_on_existing_name(self, value: str) -> None:
        h = Headers([("X-Test", "ok")])
        with pytest.raises(ValueError, match="invalid header value"):
            h.with_added("X-Test", value)

    @pytest.mark.parametrize("value", _BAD_VALUES)
    def test_with_set_rejects_control_chars(self, value: str) -> None:
        h = Headers()
        with pytest.raises(ValueError, match="invalid header value"):
            h.with_set("X-Test", value)

    @pytest.mark.parametrize("value", _BAD_VALUES)
    def test_with_set_rejects_control_chars_in_later_value(self, value: str) -> None:
        h = Headers()
        with pytest.raises(ValueError, match="invalid header value"):
            h.with_set("X-Test", "ok", value)

    @pytest.mark.parametrize("value", _BAD_VALUES)
    def test_with_merged_rejects_control_chars(self, value: str) -> None:
        base = Headers([("X-Base", "fine")])
        # Build the injected value via ``_construct`` fast-path so it bypasses
        # ``__init__`` validation, mirroring how a sibling instance could carry it.
        poisoned = object.__new__(Headers)
        object.__setattr__(poisoned, "_data", (("x-test", (value,)),))
        object.__setattr__(poisoned, "_hash", None)
        with pytest.raises(ValueError, match="invalid header value"):
            base.with_merged(poisoned)

    def test_clean_value_round_trips_through_mutators(self) -> None:
        h = Headers().with_added("X-Test", "fine").with_set("X-Other", "also-fine")
        assert h.values("x-test") == ("fine",)
        assert h.values("x-other") == ("also-fine",)


class TestHandBuiltHttpHeaderNameNormalised:
    def test_non_lowercase_value_normalised_to_lower(self) -> None:
        name = HttpHeaderName(value="Content-Type", canonical_name="Content-Type")
        assert name.value == "content-type"

    def test_non_lowercase_name_matches_lookups(self) -> None:
        name = HttpHeaderName(value="Content-Type", canonical_name="Content-Type")
        h = Headers([("content-type", "application/json")])
        assert h.get(name) == "application/json"
        assert name in h
        assert h.values(name) == ("application/json",)

    def test_non_lowercase_name_equals_lowercase_twin(self) -> None:
        upper = HttpHeaderName(value="Content-Type", canonical_name="Content-Type")
        lower = HttpHeaderName(value="content-type", canonical_name="Content-Type")
        assert upper == lower
        assert hash(upper) == hash(lower)
