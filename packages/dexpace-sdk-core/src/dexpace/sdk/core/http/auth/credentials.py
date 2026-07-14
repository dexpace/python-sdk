# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Credential Protocols and simple concrete credentials."""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

from .access_token import AccessTokenInfo, TokenRequestOptions


@runtime_checkable
class TokenCredential(Protocol):
    """Sync OAuth-style credential.

    Mirrors Azure's ``corehttp.credentials.TokenCredential``. Implementations
    are commonly stateful (caching the latest issued token) and should be
    closed when no longer needed.
    """

    def get_token_info(
        self,
        *scopes: str,
        options: TokenRequestOptions | None = None,
    ) -> AccessTokenInfo: ...

    def close(self) -> None: ...


@runtime_checkable
class AsyncTokenCredential(Protocol):
    """Async twin of ``TokenCredential``."""

    async def get_token_info(
        self,
        *scopes: str,
        options: TokenRequestOptions | None = None,
    ) -> AccessTokenInfo: ...

    async def close(self) -> None: ...


class KeyCredential:
    """Simple API-key credential.

    The key is stored opaquely and redacted in ``repr``. Use ``update`` to
    rotate the key without rebuilding the client (Azure pattern).
    """

    __slots__ = ("_key",)

    def __init__(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            raise TypeError("key must be a non-empty string")
        self._key = key

    @property
    def key(self) -> str:
        return self._key

    def update(self, key: str) -> None:
        """Replace the underlying key.

        Raises:
            ValueError: If ``key`` is empty.
            TypeError: If ``key`` is not a string.
        """
        if not isinstance(key, str):
            raise TypeError("key must be a string")
        if not key:
            raise ValueError("key must not be empty")
        self._key = key

    def __repr__(self) -> str:
        return "KeyCredential(key='[REDACTED]')"


class NamedKeyCredential:
    """Name + key pair credential (e.g. HMAC-SAS style).

    Equivalent to Azure's ``ServiceNamedKeyCredential``. Both fields are
    redacted in ``repr``. ``update`` swaps the pair atomically — a
    concurrent reader sees either both old values or both new ones, never
    a mix.
    """

    __slots__ = ("_pair",)

    def __init__(self, name: str, key: str) -> None:
        if not isinstance(name, str) or not isinstance(key, str):
            raise TypeError("both name and key must be strings")
        if not name or not key:
            raise ValueError("name and key must not be empty")
        self._pair: tuple[str, str] = (name, key)

    @property
    def name(self) -> str:
        return self._pair[0]

    @property
    def key(self) -> str:
        return self._pair[1]

    def update(self, name: str, key: str) -> None:
        """Replace name and key atomically (single tuple assignment).

        Args:
            name: New name.
            key: New key.

        Raises:
            TypeError: If either argument is not a string.
            ValueError: If either argument is empty.
        """
        if not isinstance(name, str) or not isinstance(key, str):
            raise TypeError("both name and key must be strings")
        if not name or not key:
            raise ValueError("name and key must not be empty")
        self._pair = (name, key)

    def __repr__(self) -> str:
        return "NamedKeyCredential(name='[REDACTED]', key='[REDACTED]')"


class BasicAuthCredential:
    """HTTP Basic credential — username + password, base64-encoded once.

    Both halves are validated as non-empty at construction so a blank secret
    is rejected immediately rather than silently base64-encoding an empty
    ``user:pass`` pair.
    """

    __slots__ = ("_encoded",)

    def __init__(self, username: str, password: str) -> None:
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("username and password must be strings")
        if not username or not password:
            raise ValueError("username and password must not be empty")
        raw = f"{username}:{password}".encode()
        self._encoded = base64.b64encode(raw).decode("ascii")

    @property
    def encoded(self) -> str:
        """The base64 ``user:pass`` payload, ready for an ``Authorization`` header."""
        return self._encoded

    def __repr__(self) -> str:
        return "BasicAuthCredential(username='[REDACTED]', password='[REDACTED]')"


__all__ = [
    "AsyncTokenCredential",
    "BasicAuthCredential",
    "KeyCredential",
    "NamedKeyCredential",
    "TokenCredential",
]
