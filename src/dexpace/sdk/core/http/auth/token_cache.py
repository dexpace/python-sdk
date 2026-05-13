"""Pluggable token cache for the bearer-token policy."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .access_token import AccessTokenInfo


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
    ``["b", "a"]`` map to the same entry. Every operation acquires the lock
    so the guarantee survives free-threaded CPython (PEP 703) and
    non-CPython runtimes that do not guarantee atomic dict ops.
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
        with self._lock:
            return self._entries.get(_cache_key(scopes, audience))

    def set(
        self,
        scopes: Sequence[str],
        token: AccessTokenInfo,
        audience: str | None = None,
    ) -> None:
        with self._lock:
            self._entries[_cache_key(scopes, audience)] = token

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


__all__ = ["InMemoryTokenCache", "TokenCache"]
