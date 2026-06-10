# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Cross-cutting utilities used throughout the SDK core.

Exports the `Clock` / `AsyncClock` abstractions that let
time-dependent code (retry backoff, token expiry) be driven deterministically
in tests, and the `ProxyOptions` value type used to describe outbound
HTTP / SOCKS proxies in a transport-agnostic way.
"""

from __future__ import annotations

from .clock import ASYNC_SYSTEM_CLOCK, SYSTEM_CLOCK, AsyncClock, Clock
from .proxy import ProxyOptions, ProxyType

__all__ = [
    "ASYNC_SYSTEM_CLOCK",
    "SYSTEM_CLOCK",
    "AsyncClock",
    "Clock",
    "ProxyOptions",
    "ProxyType",
]
