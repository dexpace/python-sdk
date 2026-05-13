"""OAuth-style access-token value object."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TypedDict


class TokenRequestOptions(TypedDict, total=False):
    """Optional per-request overrides forwarded to a ``TokenCredential``.

    Most credentials ignore most fields; consult your specific credential's
    documentation. The named keys here cover the Azure-style claims/tenant
    overrides used in CAE flows and the auth_flows hint from RFC 9468.
    """

    claims: str
    tenant_id: str
    auth_flows: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class AccessTokenInfo:
    """Information about an OAuth access token.

    Modelled directly on Azure ``corehttp.credentials.AccessTokenInfo``.
    ``refresh_on`` is the proactive-refresh hint published by some token
    services (the token is still valid until ``expires_on`` but should be
    refreshed earlier when possible).

    Attributes:
        token: The bearer token string.
        expires_on: Unix-time second at which the token expires.
        token_type: The scheme name (``Bearer`` for OAuth2).
        refresh_on: Unix-time second at which the token should be
            proactively refreshed; ``None`` to disable proactive refresh.
    """

    token: str
    expires_on: int
    token_type: str = "Bearer"
    refresh_on: int | None = None

    def is_expired(self, *, now: float | None = None) -> bool:
        """Return ``True`` when the token's ``expires_on`` is in the past."""
        return (now if now is not None else time.time()) >= self.expires_on

    def needs_refresh(self, *, now: float | None = None, leeway_seconds: int = 300) -> bool:
        """Return ``True`` when the token is close to (or past) expiry.

        Args:
            now: Reference time (Unix seconds); defaults to ``time.time()``.
            leeway_seconds: Refresh when token expires within this many
                seconds (default 5 minutes).

        Returns:
            ``True`` when either ``refresh_on`` has passed or
            ``expires_on - leeway_seconds`` has passed.
        """
        current = now if now is not None else time.time()
        if self.refresh_on is not None and current >= self.refresh_on:
            return True
        return current >= (self.expires_on - leeway_seconds)

    def __repr__(self) -> str:
        # Redact the token in repr — useful for logging.
        return (
            "AccessTokenInfo(token='[REDACTED]', expires_on="
            f"{self.expires_on}, token_type='{self.token_type}', "
            f"refresh_on={self.refresh_on})"
        )


__all__ = ["AccessTokenInfo", "TokenRequestOptions"]
