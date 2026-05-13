"""Per-attempt retry history record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...http.request.request import Request


@dataclass(frozen=True, slots=True)
class RequestHistory[ResponseT]:
    """Snapshot of one retried attempt.

    Captured by ``RetryPolicy`` / ``AsyncRetryPolicy`` for every failed
    attempt so callers can inspect the full retry trail on the eventual
    error (via ``ctx.data["retry_history"]``) or for post-mortem logging.

    The class is parametrised by response type: the sync chain stores
    ``RequestHistory[Response]`` and the async chain stores
    ``RequestHistory[AsyncResponse]``. Consumers can therefore inspect
    ``.response`` without runtime ``isinstance`` checks against the
    sync/async response union.

    Attributes:
        request: The request as it was sent on this attempt (may differ
            from earlier attempts if policies mutated it between retries).
        response: The response received, or ``None`` if the attempt failed
            before a response arrived.
        error: The exception raised by the attempt, or ``None`` on success.
    """

    request: Request
    response: ResponseT | None = None
    error: BaseException | None = None


__all__ = ["RequestHistory"]
