# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, settings

from dexpace.sdk.core.http.context import ContextStore

# Hypothesis profiles for the property-based suites (``test_*_properties.py``).
#
# The default "ci" profile is capped and deterministic so a normal CI run stays
# fast and never fails on timing noise: ``deadline=None`` disables the
# per-example wall-clock deadline (the usual source of flaky CI failures under
# load) and ``derandomize=True`` pins a fixed seed so runs are reproducible.
# ``too_slow`` health checks are suppressed for the same reason.
#
# TODO: a "nightly" profile (registered below with a much larger
# ``max_examples``) could run in a separate scheduled CI lane for deeper
# coverage. It is intentionally *not* wired up as the default here to keep the
# standard run quick; select it with ``HYPOTHESIS_PROFILE=nightly``.
settings.register_profile(
    "ci",
    max_examples=100,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    print_blob=True,
)
settings.register_profile(
    "nightly",
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture(autouse=True)
def _clean_context_store() -> Iterator[None]:
    """Reset the process-wide ``ContextStore`` around every test.

    Multiple tests across the suite write into ``ContextStore`` through the
    promotion chain; leaving entries in place between tests turns
    ``ContextStore.put`` collision checks into flaky failures depending on
    test ordering.
    """
    yield
    # Clear by iterating the internal dict — ContextStore exposes ``remove``
    # but no ``clear``. Touching the private attribute here is acceptable
    # for tests; production code uses the proper API.
    ContextStore._contexts.clear()


class FakeClock:
    """Deterministic `Clock` for unit tests.

    Wall-clock (``now``) and monotonic readings are tracked independently so
    tests can model the real divergence between them (system clock jumps vs
    monotonic uptime). ``sleep`` advances both. ``advance`` advances wall-
    clock by the (possibly negative) delta but only ever advances monotonic
    forward — monotonic is, by contract, non-decreasing.
    """

    __slots__ = ("_monotonic", "_wall")

    def __init__(self, start: float = 0.0) -> None:
        """Initialise both clocks at ``start`` seconds."""
        self._wall = start
        self._monotonic = start

    def now(self) -> float:
        """Return the current simulated wall-clock time, in seconds."""
        return self._wall

    def monotonic(self) -> float:
        """Return the current simulated monotonic reading, in seconds."""
        return self._monotonic

    def sleep(self, duration: float) -> None:
        """Advance both wall and monotonic by ``duration`` (clamped at zero)."""
        delta = max(0.0, duration)
        self._wall += delta
        self._monotonic += delta

    def advance(self, duration: float) -> None:
        """Advance wall-clock by ``duration`` (may be negative).

        Monotonic is only advanced when ``duration`` is positive — modelling
        the platform guarantee that monotonic time never goes backwards.
        """
        self._wall += duration
        if duration > 0:
            self._monotonic += duration


@pytest.fixture
def fake_clock() -> FakeClock:
    """Provide a fresh `FakeClock` starting at ``t=0``."""
    return FakeClock()
