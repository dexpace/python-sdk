# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure parser for server-supplied retry-pacing headers.

Single source of the ``Retry-After`` and ``X-RateLimit-Reset`` parsing shared
by the sync and async retry policies. The module is stdlib-only and free of
pipeline/error imports.

This is the extracted skeleton: it carries exactly the delta-seconds /
HTTP-date / epoch handling the retry policy has today. A later task
(PY-M4-02) fleshes out the full pacing grammar; keep new grammar work there so
this module stays the single, shared home for the parsers.
"""

from __future__ import annotations

import re
from email.utils import parsedate_to_datetime
from typing import Final

__all__ = [
    "parse_rate_limit_reset",
    "parse_retry_after",
]

#: Matches a bare ``Retry-After`` delta-seconds value (integer or decimal).
_RETRY_AFTER_DELTA_PATTERN: Final = re.compile(r"^\s*\d+(\.\d+)?\s*$")

#: Matches a bare ``X-RateLimit-Reset`` epoch value (integer or decimal).
_RATE_LIMIT_RESET_PATTERN: Final = re.compile(r"^\s*\d+(\.\d+)?\s*$")


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Parse a ``Retry-After`` header value (delta-seconds or HTTP-date).

    Args:
        value: Raw header value. ``None`` returns ``None`` directly.
        now: Current wall-clock time, in seconds since the epoch, used to
            convert an HTTP-date into a delay. Injected so the value is
            deterministic in tests.

    Returns:
        Seconds to wait (>= 0), or ``None`` when ``value`` is missing or
        unparseable.
    """
    if value is None or not value.strip():
        return None
    if _RETRY_AFTER_DELTA_PATTERN.match(value):
        return max(0.0, float(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    delta = when.timestamp() - now
    return max(0.0, delta)


def parse_rate_limit_reset(value: str | None, now: float) -> float | None:
    """Parse an ``X-RateLimit-Reset`` epoch header into a delay.

    The header carries the wall-clock second at which the rate-limit window
    resets (GitHub, Stripe, Slack). The delay is the difference between that
    instant and ``now``, floored at zero (a reset already in the past means
    retry immediately).

    Args:
        value: Raw header value (epoch seconds). ``None`` or unparseable
            returns ``None``.
        now: Current wall-clock time, in seconds since the epoch, used to
            compute the delta. Injected so the value is deterministic in tests.

    Returns:
        Seconds to wait (>= 0), or ``None`` when the header is missing or not a
        plain epoch number.
    """
    if value is None or not value.strip():
        return None
    if not _RATE_LIMIT_RESET_PATTERN.match(value):
        return None
    return max(0.0, float(value) - now)
