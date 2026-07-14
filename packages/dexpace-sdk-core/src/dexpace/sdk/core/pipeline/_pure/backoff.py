# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure exponential-backoff calculator.

Single source of the retry backoff schedule, shared by the sync and async
retry policies. The deterministic part (``compute_backoff``) is separated from
the stochastic part (``apply_jitter``) so the schedule can be reasoned about
and property-tested independently: ``compute_backoff`` is monotonic
non-decreasing in the attempt count (until it saturates at the cap), and
``apply_jitter`` layers randomness on top of that base delay.

The module is stdlib-only (``random`` for the jitter RNG) and imports nothing
from the pipeline or transport layers.
"""

from __future__ import annotations

import random
from typing import Final

__all__ = [
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_MULTIPLIER",
    "apply_jitter",
    "compute_backoff",
]

#: Default first-retry delay, in seconds, before the growth factor is applied.
DEFAULT_BASE_DELAY: Final[float] = 0.2

#: Default per-attempt growth factor for the exponential schedule.
DEFAULT_MULTIPLIER: Final[float] = 2.0

#: Default ceiling, in seconds, on a single computed backoff delay.
DEFAULT_MAX_DELAY: Final[float] = 8.0


def compute_backoff(
    attempt: int,
    *,
    base: float = DEFAULT_BASE_DELAY,
    cap: float = DEFAULT_MAX_DELAY,
    multiplier: float = DEFAULT_MULTIPLIER,
    fixed: bool = False,
) -> float:
    """Return the deterministic (pre-jitter) backoff delay for an attempt.

    The first attempt (``attempt <= 1``) waits zero seconds; from the second
    attempt on the delay is ``base`` (when ``fixed``) or
    ``base * multiplier ** (attempt - 1)`` (exponential), clamped to ``cap``.
    For ``base >= 0`` and ``multiplier >= 1`` the result is monotonic
    non-decreasing in ``attempt`` until it saturates at ``cap``.

    Args:
        attempt: 1-based attempt number (the number of attempts made so far).
        base: The base delay in seconds.
        cap: Upper bound applied to the computed delay.
        multiplier: Per-attempt growth factor for the exponential schedule.
        fixed: When ``True``, every retry waits ``base`` (clamped to ``cap``)
            instead of growing exponentially.

    Returns:
        The non-negative backoff delay in seconds, before jitter.
    """
    if attempt <= 1:
        return 0.0
    delay = base if fixed else base * multiplier ** (attempt - 1)
    return min(cap, delay)


def apply_jitter(
    delay: float,
    rng: random.Random,
    *,
    full_jitter: bool = True,
    symmetric: float = 0.0,
) -> float:
    """Apply randomised jitter to a computed backoff delay.

    A non-positive delay is returned unchanged (and draws no random sample),
    so a zero first-retry delay stays deterministic. With ``full_jitter`` the
    delay is scaled by a sample in ``[0.5, 1.0]`` (AWS's recommended scheme);
    otherwise the symmetric band scales it by ``[1 - symmetric, 1 + symmetric]``,
    and a ``symmetric`` of ``0`` yields the delay unchanged.

    Args:
        delay: The pre-jitter delay in seconds.
        rng: The random source to draw the jitter sample from.
        full_jitter: When ``True``, use the ``[0.5, 1.0]`` full-jitter band.
        symmetric: Symmetric fractional band applied when ``full_jitter`` is
            ``False``; ``0`` disables jitter.

    Returns:
        The jittered delay in seconds.
    """
    if delay <= 0.0:
        return 0.0
    if full_jitter:
        return delay * rng.uniform(0.5, 1.0)
    if symmetric == 0:
        return delay
    return delay * rng.uniform(1.0 - symmetric, 1.0 + symmetric)
