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
    """Monotonically-increasing aggregate value."""

    @abstractmethod
    def add(self, value: float = 1.0, attributes: Mapping[str, str] | None = None) -> None:
        """Increase the counter by ``value`` (must be non-negative)."""


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
    def add(self, value: float = 1.0, attributes: Mapping[str, str] | None = None) -> None:
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
