# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Transport SPI: the seam between SDK models and a concrete HTTP transport.

The `HttpClient` / `AsyncHttpClient` Protocols live here. Concrete transports
ship in separate packages (`dexpace-sdk-http-stdlib`,
`dexpace-sdk-http-httpx`, etc.) — `core` deliberately knows nothing about
specific HTTP libraries.

Two bridges cross the sync/async divide: `SyncToAsyncHttpClient` runs a blocking
transport from async code on a caller-supplied executor, and
`AsyncToSyncHttpClient` runs an async transport from sync code on a dedicated
background event-loop thread.
"""

from __future__ import annotations

from ._async_to_sync import AsyncToSyncHttpClient
from ._sync_to_async import SyncToAsyncHttpClient
from .async_http_client import AsyncHttpClient, asyncio_sleep
from .http_client import HttpClient

__all__ = [
    "AsyncHttpClient",
    "AsyncToSyncHttpClient",
    "HttpClient",
    "SyncToAsyncHttpClient",
    "asyncio_sleep",
]
