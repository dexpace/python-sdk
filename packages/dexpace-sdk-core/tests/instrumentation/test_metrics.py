# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the metrics SPI: counter/histogram semantics and no-op discards."""

from __future__ import annotations

import math
import tracemalloc
from collections.abc import Mapping

import pytest

from dexpace.sdk.core.instrumentation import (
    NOOP_COUNTER,
    NOOP_HISTOGRAM,
    NOOP_METRICS_CONTEXT,
    NOOP_UPDOWN_COUNTER,
    Counter,
    Histogram,
    UpDownCounter,
)


class _RecordingCounter(Counter):
    """A counter test double that captures the values that pass validation."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, Mapping[str, str] | None]] = []

    def _record(self, value: float, attributes: Mapping[str, str] | None) -> None:
        self.calls.append((value, attributes))


class _RecordingHistogram(Histogram):
    def __init__(self) -> None:
        self.values: list[float] = []

    def record(self, value: float, attributes: Mapping[str, str] | None = None) -> None:
        self.values.append(value)


# ----- Counter: rejects negative (OBS-33) ----------------------------------


@pytest.mark.req("OBS-33")
def test_counter_default_increment_is_one() -> None:
    counter = _RecordingCounter()
    counter.add()
    assert counter.calls == [(1.0, None)]


@pytest.mark.req("OBS-33")
def test_counter_accepts_zero_and_positive() -> None:
    counter = _RecordingCounter()
    counter.add(0.0)
    counter.add(2.5, {"route": "/v1"})
    assert counter.calls == [(0.0, None), (2.5, {"route": "/v1"})]


def test_counter_rejects_negative_increment() -> None:
    counter = _RecordingCounter()
    with pytest.raises(ValueError, match="non-negative"):
        counter.add(-1.0)
    # Rejection happens before the value reaches the recording seam.
    assert counter.calls == []


def test_noop_counter_also_rejects_negative() -> None:
    # A monotonic counter can never decrease; the invariant holds even when the
    # counter discards, so misuse surfaces whether or not metrics are enabled.
    with pytest.raises(ValueError, match="non-negative"):
        NOOP_COUNTER.add(-0.01)


@pytest.mark.req("OBS-31")
def test_noop_counter_discards_valid_increments_silently() -> None:
    NOOP_COUNTER.add()
    NOOP_COUNTER.add(3.0)
    NOOP_COUNTER.add(3.0, {"k": "v"})


# ----- UpDownCounter: allows negative --------------------------------------


def test_up_down_counter_allows_negative() -> None:
    NOOP_UPDOWN_COUNTER.add(-5.0)
    NOOP_UPDOWN_COUNTER.add(5.0, {"k": "v"})


# ----- Histogram: tolerates any input (OBS-33) -----------------------------


@pytest.mark.req("OBS-33")
def test_histogram_tolerates_any_numeric_input() -> None:
    hist = _RecordingHistogram()
    hist.record(-1.0)
    hist.record(0.0)
    hist.record(1_000_000.0)
    hist.record(math.inf)
    hist.record(math.nan)
    assert len(hist.values) == 5
    assert math.isnan(hist.values[-1])


@pytest.mark.req("OBS-33")
def test_noop_histogram_tolerates_any_input() -> None:
    NOOP_HISTOGRAM.record(-1.0)
    NOOP_HISTOGRAM.record(math.nan)
    NOOP_HISTOGRAM.record(math.inf, {"k": "v"})


# ----- Meter/MetricsContext: OTel names + units (OBS-31/32) ----------------


@pytest.mark.req("OBS-31")
def test_noop_metrics_context_builds_instruments_with_units() -> None:
    counter = NOOP_METRICS_CONTEXT.counter(
        "http.client.request.count", unit="{request}", description="requests"
    )
    hist = NOOP_METRICS_CONTEXT.histogram(
        "http.client.request.duration", unit="s", description="latency"
    )
    updown = NOOP_METRICS_CONTEXT.up_down_counter(
        "http.client.open_connections", unit="{connection}"
    )
    assert isinstance(counter, Counter)
    assert isinstance(hist, Histogram)
    assert isinstance(updown, UpDownCounter)


# ----- No-op allocation / identity (OBS-25) --------------------------------


@pytest.mark.req("OBS-31")
def test_noop_metrics_context_returns_shared_singletons() -> None:
    assert NOOP_METRICS_CONTEXT.counter("a") is NOOP_COUNTER
    assert NOOP_METRICS_CONTEXT.counter("b", unit="s") is NOOP_COUNTER
    assert NOOP_METRICS_CONTEXT.histogram("c") is NOOP_HISTOGRAM
    assert NOOP_METRICS_CONTEXT.up_down_counter("d") is NOOP_UPDOWN_COUNTER


@pytest.mark.req("OBS-20", "OBS-30")
def test_meter_callback_exception_is_not_wrapped() -> None:
    # Meter callbacks are contractually never-throwing; a raising recording seam
    # propagates unchanged rather than being wrapped or suppressed.
    sentinel = RuntimeError("meter boom")

    class _BoomCounter(Counter):
        def _record(self, value: float, attributes: Mapping[str, str] | None) -> None:
            raise sentinel

    with pytest.raises(RuntimeError) as excinfo:
        _BoomCounter().add(1.0)
    assert excinfo.value is sentinel


@pytest.mark.req("OBS-25")
def test_noop_metric_calls_retain_no_memory() -> None:
    # A genuine no-op retains nothing PER CALL: repeating the loop must not
    # grow traced memory further. The first loop is a warm-up round, not just
    # a single warm-up call — it settles any one-time cost that isn't the
    # code under test (tracemalloc's own bookkeeping table sizing itself up,
    # small-object allocator arena growth, etc.), so the measured delta
    # isolates a genuine per-call leak from a one-time constant overhead.
    add = NOOP_COUNTER.add
    record = NOOP_HISTOGRAM.record

    def run() -> None:
        for _ in range(50_000):
            add(1.0)
            record(1.0)

    tracemalloc.start()
    try:
        run()  # warm-up round: settles one-time allocator/tracemalloc costs
        before, _ = tracemalloc.get_traced_memory()
        run()
        after, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert after <= before
