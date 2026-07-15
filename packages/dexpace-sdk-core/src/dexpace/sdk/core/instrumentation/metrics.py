# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Metrics SPI — Counter / UpDownCounter / Histogram ABCs and no-op singletons.

Mirrors the OpenTelemetry-style metric primitives but stays no-deps. A
real implementation lives in a sibling package (e.g.
``dexpace-sdk-otel``); the no-op singletons cover the "tracing disabled"
case so SDK code can always emit metrics without conditional checks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Final


class Counter(ABC):
    """Monotonically-increasing aggregate value.

    The non-negative invariant is enforced here, once, for every backend:
    `add` validates the increment and then hands it to `_record`. A negative
    increment is a programming error (a monotonic counter cannot decrease) and
    is rejected even by the no-op counter, so misuse surfaces whether or not
    metrics are enabled. Use `UpDownCounter` for values that may decrease.
    """

    def add(self, value: float = 1.0, attributes: Mapping[str, str] | None = None) -> None:
        """Increase the counter by ``value``.

        Args:
            value: Non-negative increment; defaults to ``1.0``.
            attributes: Optional dimensions to attach to the measurement.

        Raises:
            ValueError: If ``value`` is negative.
        """
        if value < 0:
            raise ValueError(f"Counter increment must be non-negative, got {value!r}")
        self._record(value, attributes)

    @abstractmethod
    def _record(self, value: float, attributes: Mapping[str, str] | None) -> None:
        """Record a validated, non-negative ``value``; implemented per backend."""


class UpDownCounter(ABC):
    """Aggregate value that may go up or down (e.g. current connections)."""

    @abstractmethod
    def add(self, value: float, attributes: Mapping[str, str] | None = None) -> None:
        """Adjust the counter by ``value`` (positive or negative)."""


class Histogram(ABC):
    """Distribution of recorded values (e.g. request durations)."""

    @abstractmethod
    def record(self, value: float, attributes: Mapping[str, str] | None = None) -> None:
        """Record ``value`` into the distribution."""


class MetricsContext(ABC):
    """Factory for ``Counter`` / ``UpDownCounter`` / ``Histogram`` instances.

    Mirrors the OpenTelemetry "meter" concept: implementations build named
    instruments scoped to a particular package or component.
    """

    @abstractmethod
    def counter(
        self, name: str, *, unit: str | None = None, description: str | None = None
    ) -> Counter:
        """Build a counter instrument named ``name``."""

    @abstractmethod
    def up_down_counter(
        self,
        name: str,
        *,
        unit: str | None = None,
        description: str | None = None,
    ) -> UpDownCounter:
        """Build an up/down counter named ``name``."""

    @abstractmethod
    def histogram(
        self,
        name: str,
        *,
        unit: str | None = None,
        description: str | None = None,
    ) -> Histogram:
        """Build a histogram named ``name``."""


# ----- No-op implementations -----------------------------------------------


class _NoopCounter(Counter):
    def _record(self, value: float, attributes: Mapping[str, str] | None) -> None:
        del value, attributes


class _NoopUpDownCounter(UpDownCounter):
    def add(self, value: float, attributes: Mapping[str, str] | None = None) -> None:
        del value, attributes


class _NoopHistogram(Histogram):
    def record(self, value: float, attributes: Mapping[str, str] | None = None) -> None:
        del value, attributes


class _NoopMetricsContext(MetricsContext):
    def counter(
        self, name: str, *, unit: str | None = None, description: str | None = None
    ) -> Counter:
        del name, unit, description
        return NOOP_COUNTER

    def up_down_counter(
        self,
        name: str,
        *,
        unit: str | None = None,
        description: str | None = None,
    ) -> UpDownCounter:
        del name, unit, description
        return NOOP_UPDOWN_COUNTER

    def histogram(
        self,
        name: str,
        *,
        unit: str | None = None,
        description: str | None = None,
    ) -> Histogram:
        del name, unit, description
        return NOOP_HISTOGRAM


NOOP_COUNTER: Final[Counter] = _NoopCounter()
NOOP_UPDOWN_COUNTER: Final[UpDownCounter] = _NoopUpDownCounter()
NOOP_HISTOGRAM: Final[Histogram] = _NoopHistogram()
NOOP_METRICS_CONTEXT: Final[MetricsContext] = _NoopMetricsContext()


__all__ = [
    "NOOP_COUNTER",
    "NOOP_HISTOGRAM",
    "NOOP_METRICS_CONTEXT",
    "NOOP_UPDOWN_COUNTER",
    "Counter",
    "Histogram",
    "MetricsContext",
    "UpDownCounter",
]
