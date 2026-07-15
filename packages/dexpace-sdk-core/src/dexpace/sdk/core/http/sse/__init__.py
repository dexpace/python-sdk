# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""WHATWG-spec Server-Sent Events parsing, reconnecting client, and typed adapter."""

from __future__ import annotations

from .connection import AsyncSseConnection, SseConnection
from .parser import AsyncSseStream, SseEvent, SseParser, parse_async_events, parse_events
from .typed import AsyncTypedSseConnection, SseMapper, SseSignal, TypedSseConnection

__all__ = [
    "AsyncSseConnection",
    "AsyncSseStream",
    "AsyncTypedSseConnection",
    "SseConnection",
    "SseEvent",
    "SseMapper",
    "SseParser",
    "SseSignal",
    "TypedSseConnection",
    "parse_async_events",
    "parse_events",
]
