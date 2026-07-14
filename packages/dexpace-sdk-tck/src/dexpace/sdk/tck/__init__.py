# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Executable conformance kit (TCK) for dexpace-sdk-core transport adapters.

The public surface is the certification seam: the two harness ABCs
(`SyncTransportHarness` / `AsyncTransportHarness`), the neutral programming and
observation types the battery speaks (`Outcome`, `OutcomeKind`, `ResponseSpec`,
`SentRequest`, `DropLog`, `ClosableNative`), the self-registration registries
(`SYNC_HARNESS_FACTORIES` / `ASYNC_HARNESS_FACTORIES`), and the in-memory
reference harnesses (`InMemorySyncHarness` / `InMemoryAsyncHarness`).

A transport-adapter package certifies itself by implementing a harness against
these ABCs and appending its factory to the matching registry from its own
``tests/conftest.py``; the battery (``test_sync_contract`` / ``test_async_contract``
shipped in this package) then runs against it unchanged. Invoke the battery
directly with ``pytest --pyargs dexpace.sdk.tck``.

The golden wire-exactness byte corpus is a sibling subpackage,
`dexpace.sdk.tck.golden_corpus`; import its loaders from there.

This package depends only on core's public re-exported surface and imports no
underscore-prefixed (private) core module — an invariant enforced by
``test_import_discipline`` in this package.
"""

from __future__ import annotations

from .fake_transport import (
    FakeAsyncHttpClient,
    FakeSyncHttpClient,
    InMemoryAsyncHarness,
    InMemorySyncHarness,
)
from .harness import (
    ASYNC_HARNESS_FACTORIES,
    SYNC_HARNESS_FACTORIES,
    AsyncTransportHarness,
    ClosableNative,
    DropLog,
    Outcome,
    OutcomeKind,
    ResponseSpec,
    SentRequest,
    SyncTransportHarness,
)

__all__ = [
    "ASYNC_HARNESS_FACTORIES",
    "SYNC_HARNESS_FACTORIES",
    "AsyncTransportHarness",
    "ClosableNative",
    "DropLog",
    "FakeAsyncHttpClient",
    "FakeSyncHttpClient",
    "InMemoryAsyncHarness",
    "InMemorySyncHarness",
    "Outcome",
    "OutcomeKind",
    "ResponseSpec",
    "SentRequest",
    "SyncTransportHarness",
]
