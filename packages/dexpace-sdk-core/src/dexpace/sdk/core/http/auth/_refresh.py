# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure token refresh zone-classification and fetched-token validation.

The sync and async single-flight token caches share the "when to refresh"
and "is this fetched token usable" decisions so they live in exactly one
place. This module is deliberately free of concurrency primitives and I/O:
every function is a pure function of a token and the current wall-clock
time, which keeps the tricky threshold logic trivially testable and keeps
the two caches from drifting apart.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Final

from ...errors import ClientAuthenticationError
from .access_token import AccessTokenInfo

REFRESH_MARGIN_SECONDS: Final = 30.0
"""Default seconds before ``expires_on`` at which a token is proactively refreshed."""


class RefreshZone(Enum):
    """Freshness classification for a cached access token."""

    FRESH = auto()
    """Valid and comfortably before expiry — serve without refreshing."""

    SOFT = auto()
    """Valid but inside the refresh margin — serve, but refresh proactively."""

    HARD = auto()
    """Absent or past ``expires_on`` — a caller must block on a fresh fetch."""


def classify(
    token: AccessTokenInfo | None,
    *,
    now: float,
    margin: float = REFRESH_MARGIN_SECONDS,
) -> RefreshZone:
    """Classify a cached token into a refresh zone.

    Args:
        token: The cached token, or ``None`` when the cache is cold.
        now: Current wall-clock time in Unix seconds.
        margin: Seconds before ``expires_on`` that open the soft-refresh
            window. Defaults to `REFRESH_MARGIN_SECONDS`.

    Returns:
        `RefreshZone.HARD` when there is no token or it has expired,
        `RefreshZone.SOFT` when it is still valid but within the margin (or
        past its ``refresh_on`` hint), and `RefreshZone.FRESH` otherwise.
    """
    if token is None or now >= token.expires_on:
        return RefreshZone.HARD
    if token.refresh_on is not None and now >= token.refresh_on:
        return RefreshZone.SOFT
    if now >= token.expires_on - margin:
        return RefreshZone.SOFT
    return RefreshZone.FRESH


def validate_fetched(token: AccessTokenInfo | None, *, now: float) -> AccessTokenInfo:
    """Validate a token just returned by a provider before it is cached.

    Args:
        token: The provider's return value. A misbehaving provider may hand
            back ``None`` despite the non-optional return type.
        now: Current wall-clock time in Unix seconds, used to reject a token
            that is already expired at the moment it was fetched. No refresh
            margin is applied here: only genuine expiry is rejected, so a
            short-lived-but-valid token is accepted.

    Returns:
        The validated token, unchanged.

    Raises:
        ClientAuthenticationError: If ``token`` is ``None`` or already
            expired at ``now``.
    """
    if token is None:
        raise ClientAuthenticationError("Token provider returned no token.")
    if token.is_expired(now=now):
        raise ClientAuthenticationError("Token provider returned an already-expired token.")
    return token


__all__ = [
    "REFRESH_MARGIN_SECONDS",
    "RefreshZone",
    "classify",
    "validate_fetched",
]
