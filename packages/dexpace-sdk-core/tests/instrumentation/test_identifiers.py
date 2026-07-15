# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for W3C trace/span identifiers and their generators (OBS-26/27)."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.instrumentation import (
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)

_GENERATIONS = 10_000


@pytest.mark.req("CTX-15", "OBS-26")
def test_trace_id_invalid_sentinel_is_all_zero() -> None:
    # The documented INVALID sentinel is the all-zero id.
    assert TraceId.NOOP.value == "0" * 32
    assert TraceId.NOOP.is_valid is False


@pytest.mark.req("OBS-26")
def test_span_id_invalid_sentinel_is_all_zero() -> None:
    assert SpanId.NOOP.value == "0" * 16
    assert SpanId.NOOP.is_valid is False


@pytest.mark.req("CTX-15", "OBS-26")
def test_flags_and_state_sentinels() -> None:
    assert TraceFlags.NOOP.value == "00"
    assert TraceState.NOOP.value == ""


def test_generated_trace_id_shape() -> None:
    trace_id = TraceId.generate()
    assert len(trace_id.value) == 32
    assert trace_id.value == trace_id.value.lower()
    int(trace_id.value, 16)  # parses as hex or raises
    assert trace_id.is_valid is True


def test_generated_span_id_shape() -> None:
    span_id = SpanId.generate()
    assert len(span_id.value) == 16
    assert span_id.value == span_id.value.lower()
    int(span_id.value, 16)
    assert span_id.is_valid is True


def test_generate_never_yields_all_zero_trace_id() -> None:
    # Not just theoretically true: exercise the generator many times and prove
    # it never produces the all-zero invalid sentinel.
    for _ in range(_GENERATIONS):
        generated = TraceId.generate()
        assert generated != TraceId.NOOP
        assert generated.value != "0" * 32
        assert generated.is_valid is True


def test_generate_never_yields_all_zero_span_id() -> None:
    for _ in range(_GENERATIONS):
        generated = SpanId.generate()
        assert generated != SpanId.NOOP
        assert generated.value != "0" * 16
        assert generated.is_valid is True


def test_generated_ids_are_distinct() -> None:
    # A cheap randomness sanity check: a large sample has no collisions.
    trace_ids = {TraceId.generate().value for _ in range(1000)}
    span_ids = {SpanId.generate().value for _ in range(1000)}
    assert len(trace_ids) == 1000
    assert len(span_ids) == 1000


def test_ids_are_frozen_value_objects() -> None:
    a = TraceId("abc")
    b = TraceId("abc")
    assert a == b
    assert hash(a) == hash(b)


@pytest.mark.req("CTX-14")
def test_trace_id_type_members() -> None:
    assert set(TraceIdType) == {TraceIdType.NOOP, TraceIdType.W3C, TraceIdType.DATADOG}
    assert str(TraceIdType.W3C) == "w3c"


@pytest.mark.req("OBS-27")
def test_generate_w3c_flavour_is_32_lowercase_hex() -> None:
    # The W3C flavour is a 128-bit value rendered as 32 lowercase hex chars, and
    # the bare (argument-less) call keeps yielding it for backwards compatibility.
    for trace_id in (TraceId.generate(), TraceId.generate(TraceIdType.W3C)):
        assert len(trace_id.value) == 32
        assert trace_id.value == trace_id.value.lower()
        assert int(trace_id.value, 16) != 0  # parses as hex, never all-zero
        assert trace_id.is_valid is True


@pytest.mark.req("OBS-27")
def test_generate_datadog_flavour_is_decimal_uint64() -> None:
    for _ in range(_GENERATIONS):
        trace_id = TraceId.generate(TraceIdType.DATADOG)
        assert trace_id.value == str(int(trace_id.value))  # canonical decimal, no leading zeros
        as_int = int(trace_id.value)
        assert 1 <= as_int <= (1 << 64) - 1  # non-zero 64-bit unsigned
        assert trace_id.is_valid is True


@pytest.mark.req("OBS-27")
def test_generate_noop_flavour_always_yields_invalid_sentinel() -> None:
    for _ in range(100):
        trace_id = TraceId.generate(TraceIdType.NOOP)
        assert trace_id == TraceId.NOOP
        assert trace_id.value == "0" * 32
        assert trace_id.is_valid is False


@pytest.mark.req("OBS-27")
def test_datadog_zero_draw_is_coerced_to_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reserved all-zero id must never be produced: a zero draw is coerced.
    from dexpace.sdk.core.instrumentation import identifiers

    monkeypatch.setattr(identifiers, "_draw_datadog_bits", lambda: 0)
    trace_id = TraceId.generate(TraceIdType.DATADOG)
    assert trace_id.value == "1"
    assert trace_id.is_valid is True
