# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Wiring for the transport-adapter conformance battery.

Registers the in-memory reference harness into the shared registries and
parametrizes the ``sync_harness`` / ``async_harness`` fixtures over them at
collection time via ``pytest_generate_tests``. Reading the registries at
collection time (rather than at import time) is what makes the battery
genuinely parametrizable: each real transport-adapter package appends its own
harness factory to ``SYNC_HARNESS_FACTORIES`` / ``ASYNC_HARNESS_FACTORIES``
from ITS OWN ``tests/conftest.py`` when that package's tests are collected, and
every ``test_*`` body runs against it unchanged. This shared conftest depends
on NO adapter package — it registers only the in-memory reference — so the
battery stays transport-agnostic and lifts cleanly into a standalone TCK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .fake_transport import InMemoryAsyncHarness, InMemorySyncHarness
from .harness import (
    ASYNC_HARNESS_FACTORIES,
    SYNC_HARNESS_FACTORIES,
    AsyncTransportHarness,
    SyncTransportHarness,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def _register(registry: list[tuple[str, type]], label: str, factory: type) -> None:
    """Append ``(label, factory)`` to ``registry`` unless the label is present."""
    if not any(existing == label for existing, _ in registry):
        registry.append((label, factory))


_register(SYNC_HARNESS_FACTORIES, "in-memory", InMemorySyncHarness)
_register(ASYNC_HARNESS_FACTORIES, "in-memory", InMemoryAsyncHarness)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize the harness fixtures over the current registries."""
    if "sync_harness" in metafunc.fixturenames:
        metafunc.parametrize(
            "sync_harness",
            [pytest.param(factory, id=label) for label, factory in SYNC_HARNESS_FACTORIES],
            indirect=True,
        )
    if "async_harness" in metafunc.fixturenames:
        metafunc.parametrize(
            "async_harness",
            [pytest.param(factory, id=label) for label, factory in ASYNC_HARNESS_FACTORIES],
            indirect=True,
        )


@pytest.fixture
def sync_harness(request: pytest.FixtureRequest) -> Iterator[SyncTransportHarness]:
    """Yield a fresh synchronous harness, shutting it down on teardown."""
    harness = request.param()
    assert isinstance(harness, SyncTransportHarness)
    try:
        yield harness
    finally:
        harness.shutdown()


@pytest.fixture
def async_harness(request: pytest.FixtureRequest) -> Iterator[AsyncTransportHarness]:
    """Yield a fresh asynchronous harness, shutting it down on teardown."""
    harness = request.param()
    assert isinstance(harness, AsyncTransportHarness)
    try:
        yield harness
    finally:
        harness.shutdown()
