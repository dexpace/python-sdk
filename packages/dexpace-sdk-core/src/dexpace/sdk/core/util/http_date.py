# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""RFC 1123 HTTP-date rendering and tolerant parsing.

A single, reusable home for the HTTP-date logic that several components need:
the canonical ``Date`` / ``Expires`` render (RFC 1123 / IMF-fixdate, always in
GMT) and a tolerant parser that accepts the three date formats HTTP permits
(RFC 1123, the obsolete RFC 850, and the C ``asctime`` form) per RFC 9110.

The functions are pure — no I/O, no clock reads — so callers that already hold
a ``datetime`` can render and parse without pulling in a policy or a transport.
Parsing never raises: an unrecognisable value yields ``None`` so callers decide
how to degrade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime

__all__ = ["format_http_date", "parse_http_date"]


def format_http_date(value: datetime) -> str:
    """Render ``value`` as a canonical RFC 1123 / IMF-fixdate string in GMT.

    A naive ``datetime`` is assumed to already be UTC; an aware ``datetime`` is
    converted to UTC. The output always ends in ``GMT`` (for example
    ``Sun, 06 Nov 1994 08:49:37 GMT``), the preferred HTTP-date form.

    Args:
        value: The timestamp to render.

    Returns:
        The RFC 1123 string representation of ``value`` in GMT.
    """
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return format_datetime(aware, usegmt=True)


def parse_http_date(value: str) -> datetime | None:
    """Parse an HTTP-date string into a timezone-aware ``datetime``.

    Accepts all three HTTP-date formats (RFC 1123, RFC 850, and the C
    ``asctime`` form), which are GMT by definition. A value without an explicit
    timezone is interpreted as UTC. Parsing is tolerant: any unrecognisable or
    non-string input yields ``None`` rather than raising.

    Args:
        value: A candidate HTTP-date string.

    Returns:
        A timezone-aware ``datetime``, or ``None`` if ``value`` did not parse.
    """
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
