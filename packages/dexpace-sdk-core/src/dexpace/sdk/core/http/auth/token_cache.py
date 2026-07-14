# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pluggable token cache plus the single-flight refresh coordinators.

Three layers live here:

- `TokenCache` / `InMemoryTokenCache` — the pluggable storage seam keyed by
  ``(scopes, audience)``. The in-process default publishes an immutable dict
  snapshot so reads are wait-free (no lock on the hot path).
- `_SyncSingleFlight` / `_AsyncSingleFlight` — per-key refresh coordinators
  that wrap a storage `TokenCache`, serve the published token wait-free, and
  collapse concurrent misses into a single provider call. The async variant
  implements the three-zone (fresh / soft / hard) strategy.

The zone and validation rules the coordinators enforce are shared, pure, and
live in `_refresh`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from ...util.clock import AsyncClock, Clock
from ._refresh import REFRESH_MARGIN_SECONDS, RefreshZone, classify, validate_fetched
from .access_token import AccessTokenInfo

type _SyncProvider = Callable[[], AccessTokenInfo]
type _AsyncProvider = Callable[[], Awaitable[AccessTokenInfo]]


def _cache_key(scopes: Sequence[str], audience: str | None) -> tuple[str, ...]:
    """Build the dictionary key for a ``(scopes, audience)`` pair."""
    return (audience or "", *sorted(scopes))


@runtime_checkable
class TokenCache(Protocol):
    """Pluggable cache for ``AccessTokenInfo`` entries.

    Implementations may persist outside the process (file-backed, Redis,
    etc.). The default implementation (``InMemoryTokenCache``) is the
    in-process dict-backed variant used when no override is provided.
    """

    def get(
        self,
        scopes: Sequence[str],
        audience: str | None = None,
    ) -> AccessTokenInfo | None: ...

    def set(
        self,
        scopes: Sequence[str],
        token: AccessTokenInfo,
        audience: str | None = None,
    ) -> None: ...

    def clear(self) -> None: ...


class InMemoryTokenCache:
    """Thread-safe in-process token cache keyed by ``(scopes, audience)``.

    The scope list is sorted before being keyed so ``["a", "b"]`` and
    ``["b", "a"]`` map to the same entry.

    Reads are wait-free: ``get`` loads the current dict snapshot in a single
    attribute access and looks the key up in that immutable snapshot, taking
    no lock. Writers never mutate a published dict — ``set`` and ``clear``
    build a brand-new dict under the lock and publish it with one reference
    store. A concurrent reader therefore sees either the old snapshot or the
    new one in full, never a half-updated dict. Because publication is a
    single reference assignment (atomic on every CPython, GIL or not), the
    guarantee survives free-threaded CPython (PEP 703) and non-CPython
    runtimes that do not offer atomic dict operations.
    """

    __slots__ = ("_entries", "_lock")

    def __init__(self) -> None:
        self._entries: dict[tuple[str, ...], AccessTokenInfo] = {}
        self._lock = threading.Lock()

    def get(
        self,
        scopes: Sequence[str],
        audience: str | None = None,
    ) -> AccessTokenInfo | None:
        # Wait-free: single load of the published snapshot, then a plain
        # lookup on an immutable dict no writer will ever mutate in place.
        return self._entries.get(_cache_key(scopes, audience))

    def set(
        self,
        scopes: Sequence[str],
        token: AccessTokenInfo,
        audience: str | None = None,
    ) -> None:
        key = _cache_key(scopes, audience)
        with self._lock:
            self._entries = {**self._entries, key: token}

    def clear(self) -> None:
        with self._lock:
            self._entries = {}


class _SyncSingleFlight:
    """Wait-free reads with single-flight blocking refresh for one token key.

    The hot path loads the published token from the backing `TokenCache`
    without taking a lock (`InMemoryTokenCache.get` is wait-free). Only when
    the token is missing or no longer `RefreshZone.FRESH` does a caller take
    the lock, re-check the cache under it (double-checked locking, with no
    reliance on the GIL for atomicity), and invoke the provider — so the
    provider runs at most once per refresh window even under heavy
    concurrent pressure.
    """

    __slots__ = ("_audience", "_cache", "_clock", "_lock", "_margin", "_scopes")

    def __init__(
        self,
        cache: TokenCache,
        scopes: Sequence[str],
        audience: str | None,
        clock: Clock,
        *,
        margin: float = REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._cache = cache
        self._scopes = scopes
        self._audience = audience
        self._clock = clock
        self._margin = margin
        self._lock = threading.Lock()

    def get_token(self, provider: _SyncProvider, *, force: bool = False) -> AccessTokenInfo:
        """Return a usable token, refreshing under single-flight if needed.

        Args:
            provider: Callable that fetches a fresh token from the credential.
            force: Refresh unconditionally, ignoring any cached token.

        Returns:
            A validated, non-expired token.

        Raises:
            ClientAuthenticationError: If the provider returns ``None`` or an
                already-expired token.
        """
        token = self._cache.get(self._scopes, self._audience)  # wait-free hot read
        if not force and self._is_fresh(token):
            assert token is not None  # ``FRESH`` implies a present token.
            return token
        with self._lock:
            token = self._cache.get(self._scopes, self._audience)  # double-checked
            if not force and self._is_fresh(token):
                assert token is not None
                return token
            fetched = validate_fetched(provider(), now=self._clock.now())
            self._cache.set(self._scopes, fetched, self._audience)
            return fetched

    def _is_fresh(self, token: AccessTokenInfo | None) -> bool:
        return classify(token, now=self._clock.now(), margin=self._margin) is RefreshZone.FRESH

    def evict_if_matches(
        self,
        predicate: Callable[[AccessTokenInfo], bool],
        replacement: AccessTokenInfo,
    ) -> None:
        """Replace the cached token with ``replacement`` if it matches ``predicate``.

        Runs under the same lock ``get_token`` uses for its refresh
        compare-and-set, so eviction cannot race a concurrent refresh across
        threads: either this reads the token before a refresh publishes a new
        one (and evicts it), or it reads the new one after (and ``predicate``
        — checked against a caller-held reference to the token that was
        actually rejected — correctly declines to evict it).

        Args:
            predicate: Called with the currently cached token, if any; return
                ``True`` to evict it.
            replacement: The value to store in place of an evicted token.
        """
        with self._lock:
            token = self._cache.get(self._scopes, self._audience)
            if token is not None and predicate(token):
                self._cache.set(self._scopes, replacement, self._audience)


class _AsyncSingleFlight:
    """Three-zone async refresh with shielded single-flight for one token key.

    The hot read of the published token is wait-free. Beyond that the zone
    (see `RefreshZone`) decides what happens:

    - ``FRESH``: serve the cached token immediately.
    - ``SOFT``: serve the still-valid cached token and kick off a background
      refresh whose failure is non-fatal (the served token is still good).
    - ``HARD``: block the caller on a single shared fetch.

    The shared fetch runs as an independent task guarded by an
    ``asyncio.Lock``; every waiter awaits it through ``asyncio.shield`` so a
    single waiter's cancellation propagates to that waiter alone while the
    fetch keeps running for the others.
    """

    __slots__ = (
        "_audience",
        "_background",
        "_cache",
        "_clock",
        "_lock",
        "_margin",
        "_refresh",
        "_scopes",
    )

    def __init__(
        self,
        cache: TokenCache,
        scopes: Sequence[str],
        audience: str | None,
        clock: AsyncClock,
        *,
        margin: float = REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._cache = cache
        self._scopes = scopes
        self._audience = audience
        self._clock = clock
        self._margin = margin
        self._lock = asyncio.Lock()
        self._refresh: asyncio.Future[AccessTokenInfo] | None = None
        self._background: asyncio.Task[None] | None = None

    async def get_token(self, provider: _AsyncProvider, *, force: bool = False) -> AccessTokenInfo:
        """Return a usable token following the three-zone strategy.

        Args:
            provider: Awaitable-returning callable that fetches a fresh token.
            force: Block on a fresh fetch unconditionally, ignoring any
                cached token and its zone.

        Returns:
            A validated, non-expired token.

        Raises:
            ClientAuthenticationError: If a blocking fetch returns ``None`` or
                an already-expired token.
        """
        token = self._cache.get(self._scopes, self._audience)  # wait-free hot read
        if force:
            return await self._blocking_refresh(provider)
        zone = classify(token, now=self._clock.now(), margin=self._margin)
        if zone is RefreshZone.FRESH:
            assert token is not None
            return token
        if zone is RefreshZone.SOFT:
            assert token is not None
            self._kick_background(provider)
            return token
        return await self._blocking_refresh(provider)

    async def _blocking_refresh(self, provider: _AsyncProvider) -> AccessTokenInfo:
        """Await the shared fetch, shielded from this waiter's cancellation."""
        future = await self._ensure_refresh(provider)
        return await asyncio.shield(future)

    async def _ensure_refresh(self, provider: _AsyncProvider) -> asyncio.Future[AccessTokenInfo]:
        """Return the in-flight shared fetch, starting one if none is running."""
        async with self._lock:
            future = self._refresh
            if future is None or future.done():
                future = asyncio.ensure_future(self._fetch(provider))
                self._refresh = future
            return future

    async def _fetch(self, provider: _AsyncProvider) -> AccessTokenInfo:
        """Fetch, validate, and publish a fresh token (the shared task body)."""
        fetched = validate_fetched(await provider(), now=self._clock.now())
        self._cache.set(self._scopes, fetched, self._audience)
        return fetched

    def _kick_background(self, provider: _AsyncProvider) -> None:
        """Schedule a proactive refresh unless one is already pending."""
        if self._background is not None and not self._background.done():
            return
        self._background = asyncio.ensure_future(self._background_refresh(provider))

    async def _background_refresh(self, provider: _AsyncProvider) -> None:
        """Best-effort proactive refresh; a failure is deliberately swallowed."""
        try:
            future = await self._ensure_refresh(provider)
            await asyncio.shield(future)
        except Exception:
            # Non-fatal: the still-valid token was already served, so a failed
            # proactive refresh must never surface on the request path.
            pass


__all__ = ["InMemoryTokenCache", "TokenCache"]
