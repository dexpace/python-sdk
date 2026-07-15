# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure parser for server-supplied retry-pacing headers.

The single, shared home of the ``Retry-After``, ``retry-after-ms`` /
``x-ms-retry-after-ms``, and ``X-RateLimit-Reset`` parsing the sync and async
retry policies both consume. The module is stdlib-only apart from the shared
RFC 1123 date helper in ``util.http_date`` and carries no pipeline, transport,
or error dependencies, so it can sit below the pipeline runners in the
import-linter layering.

The policy composes these parsers in a fixed precedence (first usable wins):
``Retry-After`` delta-seconds, then ``Retry-After`` as an HTTP-date, then the
``retry-after-ms`` millisecond-delta, then the Microsoft/Azure
``x-ms-retry-after-ms`` millisecond-delta, then ``X-RateLimit-Reset`` as an
absolute Unix-epoch-seconds instant.

Two properties are load-bearing:

* **Total.** Every parser returns ``None`` on a missing or malformed value and
  never raises, for any string input. Callers treat ``None`` as "no server
  signal, fall back to the computed backoff".
* **Strict decimal grammar screened before ``float()``.** A delta-seconds or
  epoch token must be bare ASCII digits with at most one optional fractional
  part (RFC 7231 ``delta-seconds`` is ``1*DIGIT``). The screen runs *before*
  any ``float()`` call because ``float()`` is far more permissive than the
  grammar: it accepts ``nan``, ``inf``, underscore-grouped ``1_000``, a leading
  ``+``/``-`` sign, scientific ``1e3``, hex floats, and surrounding non-ASCII
  whitespace. An unscreened ``float()`` would silently admit every one of
  those; the pre-screen rejects them and yields ``None``.

A resolved delay is clamped to a 365-day sanity ceiling so an absurd or hostile
hint (a delta or HTTP-date far in the future) is capped rather than obeyed
literally. An HTTP-date already in the past floors to ``0`` (retry immediately)
— distinct from a malformed value, which yields ``None``.
"""

from __future__ import annotations

import re
from typing import Final

from ...util.http_date import parse_http_date

__all__ = [
    "MAX_RETRY_AFTER_SECONDS",
    "parse_rate_limit_reset",
    "parse_retry_after",
    "parse_retry_after_ms",
]

#: Sanity ceiling, in seconds, on a resolved pacing delay (365 days). A hint
#: beyond this is capped, not obeyed literally, so a buggy or hostile header
#: cannot make the client sleep for years.
MAX_RETRY_AFTER_SECONDS: Final = 365.0 * 24 * 60 * 60

#: Strict ``delta-seconds`` / epoch grammar: one run of ASCII digits with at
#: most one optional fractional part, optionally wrapped in ASCII spaces or
#: tabs. Anchored with ``\A`` / ``\Z`` (not ``$``) so a trailing newline cannot
#: sneak past, and ``[0-9]`` (not ``\d``) so Unicode digits are rejected.
_DECIMAL_TOKEN: Final = re.compile(r"\A[ \t]*[0-9]+(?:\.[0-9]+)?[ \t]*\Z")

#: Strict integer-milliseconds grammar for the ``retry-after-ms`` /
#: ``x-ms-retry-after-ms`` headers: one run of ASCII digits (no fractional
#: part — these headers carry whole milliseconds), optionally wrapped in ASCII
#: spaces or tabs. Anchored and ASCII-only for the same reasons as
#: ``_DECIMAL_TOKEN``.
_INTEGER_TOKEN: Final = re.compile(r"\A[ \t]*[0-9]+[ \t]*\Z")


def _screen_decimal(value: str) -> float | None:
    """Screen ``value`` against the strict decimal grammar, then ``float()`` it.

    The regex runs before ``float()`` on purpose: only bare ASCII digits with an
    optional single fractional part survive, so the follow-up ``float()`` on
    CPython always yields a finite value (or ``inf`` for an astronomically large
    magnitude, which the caller's clamp then bounds) and never raises.

    Args:
        value: The raw token to screen.

    Returns:
        The parsed non-negative seconds, or ``None`` when ``value`` is not a
        bare decimal token.
    """
    if not _DECIMAL_TOKEN.match(value):
        return None
    return float(value)


def _http_date_delay(text: str, now: float) -> float | None:
    """Convert an HTTP-date to a non-negative delay relative to ``now``.

    Delegates the tolerant RFC 1123 / RFC 850 / asctime parse to the shared
    ``util.http_date.parse_http_date`` so the date grammar lives in one place.

    Args:
        text: The candidate HTTP-date string (already stripped).
        now: Current wall-clock time in seconds since the epoch.

    Returns:
        Seconds until the parsed instant, floored at ``0`` when it is already in
        the past, or ``None`` when ``text`` is not a recognisable HTTP-date.
    """
    when = parse_http_date(text)
    if when is None:
        return None
    return max(0.0, when.timestamp() - now)


def parse_retry_after(
    value: str | None,
    now: float,
    *,
    max_delay: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    """Parse a ``Retry-After`` header value (delta-seconds or HTTP-date).

    A bare decimal token is read as delta-seconds; otherwise the value is tried
    as a tolerant HTTP-date and converted to a delay relative to ``now``. The
    resolved delay is clamped to ``max_delay``. A past HTTP-date floors to ``0``
    (retry immediately), which is distinct from a malformed value (``None``).

    Args:
        value: Raw header value. ``None``, empty, or whitespace-only yields
            ``None``.
        now: Current wall-clock time, in seconds since the epoch, used to
            convert an HTTP-date into a delay. Injected so the result is
            deterministic in tests.
        max_delay: Ceiling on the resolved delay in seconds. Defaults to the
            365-day sanity clamp; pass ``math.inf`` to disable clamping.

    Returns:
        Seconds to wait (``0 <= result <= max_delay``), or ``None`` when
        ``value`` is missing or unparseable. Never raises.
    """
    if value is None or not value.strip():
        return None
    delay = _screen_decimal(value)
    if delay is None:
        delay = _http_date_delay(value.strip(), now)
    if delay is None:
        return None
    return min(delay, max_delay)


def parse_retry_after_ms(
    value: str | None,
    *,
    max_delay: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    """Parse a millisecond-delta pacing header into a delay in seconds.

    Serves both the ``retry-after-ms`` and the Microsoft/Azure
    ``x-ms-retry-after-ms`` headers, which carry a whole-millisecond delay.
    The value is screened by a strict integer grammar before any ``float()``
    parse — only bare ASCII digits (optionally wrapped in ASCII spaces or
    tabs) survive — so ``float()``-permissive forms (``nan``, ``inf``, ``+5``,
    ``1_000``, ``1e3``, hex floats, a fractional ``1.5``) all yield ``None``
    and fall through to the next pacing signal instead of being mis-parsed.
    The resolved delay is clamped to ``max_delay``.

    Args:
        value: Raw header value (whole milliseconds). ``None``, empty, or a
            non-integer token yields ``None``.
        max_delay: Ceiling on the resolved delay in seconds. Defaults to the
            365-day sanity clamp; pass ``math.inf`` to disable clamping.

    Returns:
        Seconds to wait (``0 <= result <= max_delay``), or ``None`` when the
        header is missing or not a bare integer of milliseconds. Never raises.
    """
    if value is None or not value.strip():
        return None
    if not _INTEGER_TOKEN.match(value):
        return None
    return min(float(value) / 1000.0, max_delay)


def parse_rate_limit_reset(
    value: str | None,
    now: float,
    *,
    max_delay: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    """Parse an ``X-RateLimit-Reset`` epoch header into a delay.

    The header carries the wall-clock second at which the rate-limit window
    resets (GitHub, Stripe, Slack). The delay is the difference between that
    instant and ``now``, floored at zero (a reset already in the past means
    retry immediately) and clamped to ``max_delay``. The value is screened by
    the same strict decimal grammar as ``Retry-After`` delta-seconds before any
    ``float()`` parse.

    Args:
        value: Raw header value (epoch seconds). ``None``, empty, or a
            non-decimal token yields ``None``.
        now: Current wall-clock time, in seconds since the epoch, used to
            compute the delta. Injected so the result is deterministic in tests.
        max_delay: Ceiling on the resolved delay in seconds. Defaults to the
            365-day sanity clamp; pass ``math.inf`` to disable clamping.

    Returns:
        Seconds to wait (``0 <= result <= max_delay``), or ``None`` when the
        header is missing or not a plain epoch number. Never raises.
    """
    if value is None or not value.strip():
        return None
    epoch = _screen_decimal(value)
    if epoch is None:
        return None
    return min(max(0.0, epoch - now), max_delay)
