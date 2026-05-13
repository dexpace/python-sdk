"""Cross-cutting utilities used throughout the SDK core.

Currently exports the :class:`Clock` / :class:`AsyncClock` abstractions
that let time-dependent code (retry backoff, token expiry) be driven
deterministically in tests.
"""

from __future__ import annotations

from .clock import ASYNC_SYSTEM_CLOCK, SYSTEM_CLOCK, AsyncClock, Clock

__all__ = [
    "ASYNC_SYSTEM_CLOCK",
    "SYSTEM_CLOCK",
    "AsyncClock",
    "Clock",
]
