"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from dexpace.sdk.core.http.context import ContextStore


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
    """Deterministic :class:`~dexpace.sdk.core.util.Clock` for unit tests.

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
    """Provide a fresh :class:`FakeClock` starting at ``t=0``."""
    return FakeClock()
