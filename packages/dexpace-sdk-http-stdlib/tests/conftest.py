# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Registers the real-stdlib harnesses into the shared transport-adapter
conformance battery (the installed ``dexpace.sdk.tck`` package).

The battery's registries (``SYNC_HARNESS_FACTORIES`` / ``ASYNC_HARNESS_FACTORIES``)
are plain module-level lists on ``dexpace.sdk.tck.harness`` — the same object
regardless of which package imports it, for the lifetime of one pytest process.
Appending this package's harness factories here is enough for every ``test_*``
body in that shared battery to run against the real ``UrllibHttpClient`` /
``AsyncioHttpClient`` too, with no change to the battery itself.

The harness ABCs live in the installed ``dexpace.sdk.tck`` distribution, so
resolving them is an ordinary install-based import. This package's own
``stdlib_conformance_harness`` module, however, is a bare module in this test
directory; the ``sys.path`` insertion below makes it importable regardless of
pytest's import mode (``consider_namespace_packages`` stops pytest from adding a
plain test directory to ``sys.path`` on its own). Keeping the stdlib harness in
this package — rather than in the TCK — is what keeps the shared battery
transport-agnostic (it depends on no adapter package), matching the httpx /
requests / aiohttp adapters.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from dexpace.sdk.tck.harness import (  # noqa: E402
    ASYNC_HARNESS_FACTORIES,
    SYNC_HARNESS_FACTORIES,
)
from stdlib_conformance_harness import StdlibAsyncHarness, StdlibSyncHarness  # noqa: E402


def _register(registry: list[tuple[str, type]], label: str, factory: type) -> None:
    """Append ``(label, factory)`` to ``registry`` unless the label is present."""
    if not any(existing == label for existing, _ in registry):
        registry.append((label, factory))


_register(SYNC_HARNESS_FACTORIES, "stdlib-urllib", StdlibSyncHarness)
_register(ASYNC_HARNESS_FACTORIES, "stdlib-asyncio", StdlibAsyncHarness)
