# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Registers the real-``requests`` harness into the shared transport-adapter
conformance battery (``dexpace-sdk-core/tests/transport_conformance``).

The battery's registries (``SYNC_HARNESS_FACTORIES`` / ``ASYNC_HARNESS_FACTORIES``)
are plain module-level lists on ``tests.transport_conformance.harness`` — the
same object regardless of which package imports it, for the lifetime of one
pytest process. Appending this package's harness factory here is enough for
every ``test_*`` body in that shared battery to run against it too, with no
change to the battery itself.

``packages/dexpace-sdk-core`` is loaded before this package in the workspace's
``testpaths`` order, but the ``sys.path`` insertion below is done explicitly
(rather than relied upon implicitly) so this registration also works when
this package's tests are collected on their own.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CORE_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "dexpace-sdk-core"
if str(_CORE_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_PACKAGE_ROOT))

from tests.transport_conformance.harness import SYNC_HARNESS_FACTORIES  # noqa: E402

from requests_harness import RequestsSyncHarness  # noqa: E402


def _register(registry: list[tuple[str, type]], label: str, factory: type) -> None:
    """Append ``(label, factory)`` to ``registry`` unless the label is present."""
    if not any(existing == label for existing, _ in registry):
        registry.append((label, factory))


_register(SYNC_HARNESS_FACTORIES, "requests", RequestsSyncHarness)
