# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Registers the real-``requests`` harness into the shared transport-adapter
conformance battery (the installed ``dexpace.sdk.tck`` package).

The battery's registries (``SYNC_HARNESS_FACTORIES`` / ``ASYNC_HARNESS_FACTORIES``)
are plain module-level lists on ``dexpace.sdk.tck.harness`` — the same object
regardless of which package imports it, for the lifetime of one pytest process.
Appending this package's harness factory here is enough for every ``test_*``
body in that shared battery to run against it too, with no change to the
battery itself.

The harness ABCs resolve from the installed ``dexpace.sdk.tck`` distribution as
an ordinary import; this package's own ``requests_harness`` module is a bare
module in this test directory, so the ``sys.path`` insertion below makes it
importable regardless of pytest's import mode (``consider_namespace_packages``
stops pytest from adding a plain test directory to ``sys.path`` on its own).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from dexpace.sdk.tck.harness import SYNC_HARNESS_FACTORIES  # noqa: E402
from requests_harness import RequestsSyncHarness  # noqa: E402


def _register(registry: list[tuple[str, type]], label: str, factory: type) -> None:
    """Append ``(label, factory)`` to ``registry`` unless the label is present."""
    if not any(existing == label for existing, _ in registry):
        registry.append((label, factory))


_register(SYNC_HARNESS_FACTORIES, "requests", RequestsSyncHarness)
