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

    Wall-clock and monotonic readings are backed by the same internal
    counter, which advances only via :meth:`sleep` (clamped at zero) or
    explicit :meth:`advance` calls. No real time elapses.
    """

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        """Initialise the fake clock at ``start`` seconds."""
        self._t = start

    def now(self) -> float:
        """Return the current simulated wall-clock time, in seconds."""
        return self._t

    def monotonic(self) -> float:
        """Return the current simulated monotonic reading, in seconds."""
        return self._t

    def sleep(self, duration: float) -> None:
        """Advance the clock by ``duration`` seconds, clamped at zero."""
        self._t += max(0.0, duration)

    def advance(self, duration: float) -> None:
        """Advance the clock by ``duration`` seconds (may be negative)."""
        self._t += duration


@pytest.fixture
def fake_clock() -> FakeClock:
    """Provide a fresh :class:`FakeClock` starting at ``t=0``."""
    return FakeClock()
