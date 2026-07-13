# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the single-flight token caches and their shared refresh logic.

Exercises the wait-free hot read, single-flight refresh under real
concurrency, the async three-zone strategy (fresh / soft / hard), the
non-fatal background-refresh path, the misbehaving-provider matrix, and the
waiter-cancel guarantee.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest

from dexpace.sdk.core.errors import ClientAuthenticationError
from dexpace.sdk.core.http.auth._refresh import (
    REFRESH_MARGIN_SECONDS,
    RefreshZone,
    classify,
    validate_fetched,
)
from dexpace.sdk.core.http.auth.access_token import AccessTokenInfo
from dexpace.sdk.core.http.auth.token_cache import (
    InMemoryTokenCache,
    _AsyncSingleFlight,
    _SyncSingleFlight,
)
from dexpace.sdk.core.util.clock import SYSTEM_CLOCK

from ..conftest import FakeClock


class _AsyncStepClock:
    """Deterministic ``AsyncClock`` whose time only moves when told to.

    ``now`` is fixed at ``start`` unless ``advance``/``sleep`` moves it, so a
    test can pin a token in a specific refresh zone.
    """

    __slots__ = ("_now",)

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, duration: float) -> None:
        if duration > 0:
            self._now += duration

    def advance(self, duration: float) -> None:
        self._now += duration


# --------------------------------------------------------------------------- #
# Pure zone classification + fetched-token validation                          #
# --------------------------------------------------------------------------- #


def test_refresh_margin_default_is_30_seconds() -> None:
    assert REFRESH_MARGIN_SECONDS == 30.0


def test_classify_zone_boundaries() -> None:
    now = 1000.0
    # No token or past expiry -> HARD (block on fetch).
    assert classify(None, now=now) is RefreshZone.HARD
    assert classify(AccessTokenInfo("t", 1000), now=now) is RefreshZone.HARD
    # Valid but inside the 30s margin -> SOFT.
    assert classify(AccessTokenInfo("t", 1020), now=now) is RefreshZone.SOFT
    # Exactly on the margin boundary (now == expires_on - margin) -> SOFT.
    assert classify(AccessTokenInfo("t", 1030), now=now) is RefreshZone.SOFT
    # One second before the margin opens -> FRESH.
    assert classify(AccessTokenInfo("t", 1031), now=now) is RefreshZone.FRESH
    # Comfortably valid -> FRESH.
    assert classify(AccessTokenInfo("t", 5000), now=now) is RefreshZone.FRESH
    # A passed ``refresh_on`` hint opens the soft window even when far from expiry.
    assert classify(AccessTokenInfo("t", 5000, refresh_on=900), now=now) is RefreshZone.SOFT


def test_validate_fetched_rejects_none_and_already_expired() -> None:
    with pytest.raises(ClientAuthenticationError):
        validate_fetched(None, now=1000.0)
    # Already expired at fetch time (no margin applied) -> error.
    with pytest.raises(ClientAuthenticationError):
        validate_fetched(AccessTokenInfo("t", 1000), now=1000.0)
    # Inside the margin but not expired: valid, because no margin is applied here.
    ok = validate_fetched(AccessTokenInfo("t", 1005), now=1000.0)
    assert ok.token == "t"


# --------------------------------------------------------------------------- #
# Sync single-flight cache                                                     #
# --------------------------------------------------------------------------- #


def test_sync_single_flight_one_fetch_under_thread_pool() -> None:
    """N concurrent cache misses collapse to exactly one provider fetch."""
    cache = InMemoryTokenCache()
    lock = threading.Lock()
    calls = 0

    def provider() -> AccessTokenInfo:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.02)
        return AccessTokenInfo(token="t", expires_on=int(time.time()) + 3600)

    sf = _SyncSingleFlight(cache, ("s",), None, SYSTEM_CLOCK)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(sf.get_token, provider) for _ in range(8)]
        results = [f.result(timeout=5) for f in futures]

    assert calls == 1
    assert all(r.token == "t" for r in results)


def test_sync_fresh_token_served_without_fetch() -> None:
    clock = FakeClock(start=1000.0)
    cache = InMemoryTokenCache()
    cache.set(("s",), AccessTokenInfo(token="cached", expires_on=1000 + 3600))
    sf = _SyncSingleFlight(cache, ("s",), None, clock)

    def provider() -> AccessTokenInfo:
        raise AssertionError("provider must not be called for a fresh token")

    assert sf.get_token(provider).token == "cached"


def test_sync_none_token_raises() -> None:
    sf = _SyncSingleFlight(InMemoryTokenCache(), ("s",), None, SYSTEM_CLOCK)

    def provider() -> AccessTokenInfo:
        return cast("AccessTokenInfo", None)

    with pytest.raises(ClientAuthenticationError):
        sf.get_token(provider)


def test_sync_already_expired_token_raises() -> None:
    clock = FakeClock(start=1000.0)
    sf = _SyncSingleFlight(InMemoryTokenCache(), ("s",), None, clock)

    def provider() -> AccessTokenInfo:
        return AccessTokenInfo(token="t", expires_on=1000)

    with pytest.raises(ClientAuthenticationError):
        sf.get_token(provider)


def test_sync_throwing_provider_not_cached_and_retried() -> None:
    cache = InMemoryTokenCache()
    calls = 0

    def provider() -> AccessTokenInfo:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("token endpoint down")
        return AccessTokenInfo(token="t", expires_on=int(time.time()) + 3600)

    sf = _SyncSingleFlight(cache, ("s",), None, SYSTEM_CLOCK)
    with pytest.raises(RuntimeError):
        sf.get_token(provider)
    # A throwing provider must not be cached: the next call retries it.
    assert cache.get(("s",)) is None
    assert sf.get_token(provider).token == "t"
    assert calls == 2


# --------------------------------------------------------------------------- #
# Async single-flight cache — three-zone strategy                             #
# --------------------------------------------------------------------------- #


async def test_async_single_flight_one_fetch_under_gather() -> None:
    """N concurrent cache misses collapse to exactly one provider fetch."""
    clock = _AsyncStepClock(start=1000.0)
    cache = InMemoryTokenCache()
    calls = 0

    async def provider() -> AccessTokenInfo:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return AccessTokenInfo(token="t", expires_on=int(clock.now()) + 3600)

    sf = _AsyncSingleFlight(cache, ("s",), None, clock)
    results = await asyncio.gather(*(sf.get_token(provider) for _ in range(8)))

    assert calls == 1
    assert all(r.token == "t" for r in results)


async def test_async_fresh_zone_serves_without_fetch() -> None:
    clock = _AsyncStepClock(start=1000.0)
    cache = InMemoryTokenCache()
    cache.set(("s",), AccessTokenInfo(token="fresh", expires_on=1000 + 3600))
    sf = _AsyncSingleFlight(cache, ("s",), None, clock)

    async def provider() -> AccessTokenInfo:
        raise AssertionError("provider must not be called for a fresh token")

    assert (await sf.get_token(provider)).token == "fresh"


async def test_async_soft_zone_serves_stale_and_refreshes_in_background() -> None:
    clock = _AsyncStepClock(start=1000.0)
    cache = InMemoryTokenCache()
    # Expires in 20s: still valid, but inside the 30s margin -> SOFT.
    cache.set(("s",), AccessTokenInfo(token="stale", expires_on=1020))
    calls = 0

    async def provider() -> AccessTokenInfo:
        nonlocal calls
        calls += 1
        return AccessTokenInfo(token="fresh", expires_on=int(clock.now()) + 3600)

    sf = _AsyncSingleFlight(cache, ("s",), None, clock)
    served = await sf.get_token(provider)

    # The still-valid token is served immediately.
    assert served.token == "stale"
    # A background refresh was scheduled; drain it deterministically.
    assert sf._background is not None
    await sf._background
    assert calls == 1
    entry = cache.get(("s",))
    assert entry is not None and entry.token == "fresh"


async def test_async_background_refresh_failure_is_non_fatal() -> None:
    clock = _AsyncStepClock(start=1000.0)
    cache = InMemoryTokenCache()
    cache.set(("s",), AccessTokenInfo(token="stale", expires_on=1020))  # SOFT

    async def provider() -> AccessTokenInfo:
        raise RuntimeError("token endpoint down")

    sf = _AsyncSingleFlight(cache, ("s",), None, clock)
    # Serving the still-valid token must not raise despite the doomed refresh.
    served = await sf.get_token(provider)
    assert served.token == "stale"

    assert sf._background is not None
    await sf._background  # completes without raising (failure swallowed)
    # The cache still holds the previously-valid token.
    entry = cache.get(("s",))
    assert entry is not None and entry.token == "stale"


async def test_async_hard_zone_blocks_on_fetch() -> None:
    clock = _AsyncStepClock(start=1000.0)
    cache = InMemoryTokenCache()
    cache.set(("s",), AccessTokenInfo(token="expired", expires_on=1000))  # HARD

    async def provider() -> AccessTokenInfo:
        return AccessTokenInfo(token="fresh", expires_on=int(clock.now()) + 3600)

    sf = _AsyncSingleFlight(cache, ("s",), None, clock)
    assert (await sf.get_token(provider)).token == "fresh"


async def test_async_none_token_raises() -> None:
    sf = _AsyncSingleFlight(InMemoryTokenCache(), ("s",), None, _AsyncStepClock(1000.0))

    async def provider() -> AccessTokenInfo:
        return cast("AccessTokenInfo", None)

    with pytest.raises(ClientAuthenticationError):
        await sf.get_token(provider)


async def test_async_already_expired_token_raises() -> None:
    clock = _AsyncStepClock(start=1000.0)
    sf = _AsyncSingleFlight(InMemoryTokenCache(), ("s",), None, clock)

    async def provider() -> AccessTokenInfo:
        return AccessTokenInfo(token="t", expires_on=1000)

    with pytest.raises(ClientAuthenticationError):
        await sf.get_token(provider)


async def test_async_throwing_provider_not_cached_and_retried() -> None:
    clock = _AsyncStepClock(start=1000.0)
    cache = InMemoryTokenCache()
    calls = 0

    async def provider() -> AccessTokenInfo:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return AccessTokenInfo(token="t", expires_on=int(clock.now()) + 3600)

    sf = _AsyncSingleFlight(cache, ("s",), None, clock)
    with pytest.raises(RuntimeError):
        await sf.get_token(provider)
    assert cache.get(("s",)) is None
    assert (await sf.get_token(provider)).token == "t"
    assert calls == 2


async def test_async_cancelled_waiter_does_not_kill_shared_fetch() -> None:
    """One waiter's cancellation propagates to it alone; others still win."""
    clock = _AsyncStepClock(start=1000.0)
    cache = InMemoryTokenCache()  # cold -> HARD for all waiters
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider() -> AccessTokenInfo:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return AccessTokenInfo(token="t", expires_on=int(clock.now()) + 3600)

    sf = _AsyncSingleFlight(cache, ("s",), None, clock)
    w1 = asyncio.ensure_future(sf.get_token(provider))
    w2 = asyncio.ensure_future(sf.get_token(provider))
    w3 = asyncio.ensure_future(sf.get_token(provider))

    await started.wait()  # the single shared fetch is in flight
    w2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w2

    release.set()
    tokens = await asyncio.gather(w1, w3)
    assert all(t.token == "t" for t in tokens)
    # The shared fetch survived the one cancellation and ran exactly once.
    assert calls == 1
