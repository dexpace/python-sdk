"""Clock abstractions for testable, time-dependent code.

Time-dependent components (retry backoff, bearer-token refresh, deadline
arithmetic) accept a ``Clock`` / ``AsyncClock`` rather than calling
``time.time`` / ``time.sleep`` / ``asyncio.sleep`` directly. Production
code wires in :data:`SYSTEM_CLOCK` or :data:`ASYNC_SYSTEM_CLOCK`; tests
substitute a deterministic fake (see ``tests/conftest.py::FakeClock``).

Both protocols are ``@runtime_checkable`` so callers can assert
conformance via ``isinstance`` at the seams without taking a hard
dependency on the concrete implementation.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable

__all__ = [
    "ASYNC_SYSTEM_CLOCK",
    "SYSTEM_CLOCK",
    "AsyncClock",
    "Clock",
]


@runtime_checkable
class Clock(Protocol):
    """Source of wall-clock time, monotonic time, and blocking sleep.

    Injected into time-dependent components (retry backoff, bearer-token
    expiry) so tests can drive time deterministically without real
    sleeps. ``sleep`` on a zero-or-negative duration is a no-op.
    """

    def now(self) -> float:
        """Return the current wall-clock time, in seconds since the epoch."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic time reading, in seconds.

        The absolute value is meaningless; only differences between
        successive readings are defined.
        """
        ...

    def sleep(self, duration: float) -> None:
        """Block for ``duration`` seconds.

        Args:
            duration: Seconds to block. Zero or negative values return
                immediately without raising.
        """
        ...


@runtime_checkable
class AsyncClock(Protocol):
    """Async twin of :class:`Clock` for use inside ``async def`` callers.

    ``now`` / ``monotonic`` remain synchronous — they are cheap reads
    against the operating system. Only ``sleep`` is awaitable.
    """

    def now(self) -> float:
        """Return the current wall-clock time, in seconds since the epoch."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic time reading, in seconds."""
        ...

    async def sleep(self, duration: float) -> None:
        """Yield to the event loop for ``duration`` seconds.

        Args:
            duration: Seconds to sleep. Zero or negative values return
                immediately without yielding.
        """
        ...


class _SystemClock:
    """Default :class:`Clock` backed by :mod:`time`."""

    __slots__ = ()

    def now(self) -> float:
        """Return ``time.time()``."""
        return time.time()

    def monotonic(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()

    def sleep(self, duration: float) -> None:
        """Delegate to ``time.sleep`` for positive durations only."""
        if duration > 0:
            time.sleep(duration)


class _AsyncSystemClock:
    """Default :class:`AsyncClock` backed by :mod:`asyncio`."""

    __slots__ = ()

    def now(self) -> float:
        """Return ``time.time()``."""
        return time.time()

    def monotonic(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()

    async def sleep(self, duration: float) -> None:
        """Delegate to ``asyncio.sleep`` for positive durations only."""
        if duration > 0:
            await asyncio.sleep(duration)


SYSTEM_CLOCK: Clock = _SystemClock()
"""Process-wide default :class:`Clock` backed by the standard library."""

ASYNC_SYSTEM_CLOCK: AsyncClock = _AsyncSystemClock()
"""Process-wide default :class:`AsyncClock` backed by :mod:`asyncio`."""
