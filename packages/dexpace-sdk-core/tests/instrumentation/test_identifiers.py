# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for W3C trace/span identifiers and their generators (OBS-26/27)."""

from __future__ import annotations

from dexpace.sdk.core.instrumentation import (
    SpanId,
    TraceFlags,
    TraceId,
    TraceIdType,
    TraceState,
)

_GENERATIONS = 10_000


def test_trace_id_invalid_sentinel_is_all_zero() -> None:
    # The documented INVALID sentinel is the all-zero id.
    assert TraceId.NOOP.value == "0" * 32
    assert TraceId.NOOP.is_valid is False


def test_span_id_invalid_sentinel_is_all_zero() -> None:
    assert SpanId.NOOP.value == "0" * 16
    assert SpanId.NOOP.is_valid is False


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


def test_trace_id_type_members() -> None:
    assert set(TraceIdType) == {TraceIdType.NOOP, TraceIdType.W3C, TraceIdType.DATADOG}
    assert str(TraceIdType.W3C) == "w3c"
