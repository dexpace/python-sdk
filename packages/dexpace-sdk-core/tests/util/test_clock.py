# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the `dexpace.sdk.core.util.clock` module."""

from __future__ import annotations

import threading
import time
from itertools import pairwise

import pytest

from dexpace.sdk.core.util import (
    ASYNC_SYSTEM_CLOCK,
    SYSTEM_CLOCK,
    AsyncClock,
    CancellationToken,
    Clock,
)
from dexpace.sdk.core.util.clock import _AsyncSystemClock, _SystemClock

from ..conftest import FakeClock


def test_system_clock_now_advances() -> None:
    """``now()`` returns wall-clock time that advances across a sleep."""
    first = SYSTEM_CLOCK.now()
    time.sleep(0.01)
    second = SYSTEM_CLOCK.now()
    assert second > first


def test_system_clock_monotonic_non_decreasing() -> None:
    """``monotonic()`` never goes backwards across repeated reads."""
    samples = [SYSTEM_CLOCK.monotonic() for _ in range(50)]
    for earlier, later in pairwise(samples):
        assert later >= earlier


def test_system_clock_sleep_zero_is_noop() -> None:
    """``sleep(0)`` is a no-op — does not raise, returns promptly."""
    start = time.monotonic()
    SYSTEM_CLOCK.sleep(0.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


def test_system_clock_sleep_negative_is_noop() -> None:
    """``sleep(-1)`` is a no-op (matches the Java SDK contract)."""
    start = time.monotonic()
    SYSTEM_CLOCK.sleep(-1.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


def test_system_clock_sleep_positive_actually_sleeps() -> None:
    """``sleep(d)`` for ``d > 0`` blocks for approximately ``d`` seconds."""
    start = time.monotonic()
    SYSTEM_CLOCK.sleep(0.05)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04


async def test_async_system_clock_sleep() -> None:
    """Awaiting ``sleep(d)`` for ``d > 0`` yields for ~the requested duration."""
    start = time.monotonic()
    await ASYNC_SYSTEM_CLOCK.sleep(0.05)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04


async def test_async_system_clock_sleep_zero_is_noop() -> None:
    """Awaiting ``sleep(0)`` is a no-op — returns promptly without raising."""
    start = time.monotonic()
    await ASYNC_SYSTEM_CLOCK.sleep(0.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


async def test_async_system_clock_sleep_negative_is_noop() -> None:
    """Awaiting ``sleep(-1)`` is a no-op."""
    start = time.monotonic()
    await ASYNC_SYSTEM_CLOCK.sleep(-1.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


def test_async_system_clock_now_and_monotonic() -> None:
    """``AsyncClock`` exposes synchronous ``now`` / ``monotonic`` accessors."""
    assert ASYNC_SYSTEM_CLOCK.now() > 0
    a = ASYNC_SYSTEM_CLOCK.monotonic()
    b = ASYNC_SYSTEM_CLOCK.monotonic()
    assert b >= a


def test_clock_protocol_satisfied_by_system_clock() -> None:
    """``SYSTEM_CLOCK`` structurally satisfies the `Clock` protocol."""
    assert isinstance(SYSTEM_CLOCK, Clock)
    assert isinstance(_SystemClock(), Clock)


def test_async_clock_protocol_satisfied_by_async_system_clock() -> None:
    """``ASYNC_SYSTEM_CLOCK`` structurally satisfies `AsyncClock`."""
    assert isinstance(ASYNC_SYSTEM_CLOCK, AsyncClock)
    assert isinstance(_AsyncSystemClock(), AsyncClock)


def test_fake_clock_advance(fake_clock: FakeClock) -> None:
    """``FakeClock`` advances time via both ``sleep`` and explicit ``advance``."""
    assert fake_clock.now() == 0.0
    assert fake_clock.monotonic() == 0.0

    fake_clock.sleep(1.5)
    assert fake_clock.now() == pytest.approx(1.5)
    assert fake_clock.monotonic() == pytest.approx(1.5)

    fake_clock.advance(2.0)
    assert fake_clock.now() == pytest.approx(3.5)

    # Negative / zero sleep is clamped to zero — no time travel backwards.
    fake_clock.sleep(-10.0)
    assert fake_clock.now() == pytest.approx(3.5)
    fake_clock.sleep(0.0)
    assert fake_clock.now() == pytest.approx(3.5)

    # ``advance`` accepts negatives explicitly (test-only escape hatch).
    fake_clock.advance(-0.5)
    assert fake_clock.now() == pytest.approx(3.0)


def test_fake_clock_satisfies_clock_protocol(fake_clock: FakeClock) -> None:
    """``FakeClock`` is structurally a `Clock`."""
    assert isinstance(fake_clock, Clock)


def test_fake_clock_monotonic_does_not_decrease_on_negative_advance() -> None:
    """``advance(-delta)`` moves wall back but monotonic is preserved."""
    clock = FakeClock(start=10.0)
    clock.advance(-3.0)
    assert clock.now() == pytest.approx(7.0)
    # Monotonic time never goes backwards on real systems.
    assert clock.monotonic() == pytest.approx(10.0)


def test_fake_clock_sleep_advances_both_wall_and_monotonic() -> None:
    clock = FakeClock()
    clock.sleep(5.0)
    assert clock.now() == pytest.approx(5.0)
    assert clock.monotonic() == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# CancellationToken — interruptible sleep


def test_cancellation_token_starts_uncancelled() -> None:
    token = CancellationToken()
    assert token.cancelled is False


def test_cancellation_token_cancel_sets_flag() -> None:
    token = CancellationToken()
    token.cancel()
    assert token.cancelled is True


def test_cancellation_token_cancel_is_idempotent() -> None:
    token = CancellationToken()
    token.cancel()
    token.cancel()
    assert token.cancelled is True


def test_sleep_completes_when_not_cancelled() -> None:
    """A short sleep runs to completion and reports ``True``."""
    token = CancellationToken()
    start = time.monotonic()
    completed = token.sleep(0.05)
    elapsed = time.monotonic() - start
    assert completed is True
    assert elapsed >= 0.04


def test_sleep_returns_immediately_when_already_cancelled() -> None:
    token = CancellationToken()
    token.cancel()
    start = time.monotonic()
    completed = token.sleep(10.0)
    elapsed = time.monotonic() - start
    assert completed is False
    assert elapsed < 0.05


def test_sleep_wakes_early_when_cancelled_from_another_thread() -> None:
    token = CancellationToken()
    threading.Timer(0.02, token.cancel).start()
    start = time.monotonic()
    completed = token.sleep(10.0)
    elapsed = time.monotonic() - start
    assert completed is False
    # Cancelled well before the 10s budget would have elapsed.
    assert elapsed < 1.0


def test_sleep_zero_is_noop_and_completes() -> None:
    token = CancellationToken()
    assert token.sleep(0.0) is True


def test_sleep_rejects_negative_duration() -> None:
    token = CancellationToken()
    with pytest.raises(ValueError):
        token.sleep(-1.0)


def test_sleep_rejects_nan_duration() -> None:
    token = CancellationToken()
    with pytest.raises(ValueError):
        token.sleep(float("nan"))
