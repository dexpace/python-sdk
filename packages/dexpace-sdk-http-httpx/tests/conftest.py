# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Registers the httpx harnesses into the shared transport-conformance battery.

The TCK's ``dexpace.sdk.tck`` conftest parametrizes the ``sync_harness`` /
``async_harness`` fixtures over ``SYNC_HARNESS_FACTORIES`` /
``ASYNC_HARNESS_FACTORIES`` at collection time. Appending this package's
factories here — rather than editing the TCK — keeps this certification's
changes confined to ``dexpace-sdk-http-httpx``, so it never collides with the
equivalent registration a sibling transport adds to its own package.

Registration only has an effect when the TCK's battery is part of the same
pytest session (the normal case: the workspace's ``testpaths`` collects the TCK
battery and every adapter package's ``tests/`` in one run, and all
distributions share one editable-install virtualenv, so
``dexpace.sdk.tck.harness`` is the very same module object regardless of which
package's conftest imports it first). Running this package's tests in isolation
without the TCK installed never loads that module, so both the harness module
and this registration are best-effort: this package's own tests still run
standalone, just without the shared battery.
"""

from __future__ import annotations

import sys
from pathlib import Path

# This package's own ``transport_conformance_harness`` is a bare module in this
# test directory; adding the directory to ``sys.path`` makes it importable
# regardless of pytest's import mode (``consider_namespace_packages`` stops
# pytest from adding a plain test directory to ``sys.path`` on its own).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_Registry = list[tuple[str, type]]

_registries: tuple[_Registry, _Registry] | None
try:
    from dexpace.sdk.tck.harness import (
        ASYNC_HARNESS_FACTORIES,
        SYNC_HARNESS_FACTORIES,
    )
    from transport_conformance_harness import HttpxAsyncHarness, HttpxSyncHarness

    _registries = (SYNC_HARNESS_FACTORIES, ASYNC_HARNESS_FACTORIES)
except ModuleNotFoundError:
    _registries = None


def _register(registry: _Registry, label: str, factory: type) -> None:
    """Append ``(label, factory)`` to ``registry`` unless the label is present."""
    if not any(existing == label for existing, _ in registry):
        registry.append((label, factory))


if _registries is not None:
    _sync_registry, _async_registry = _registries
    _register(_sync_registry, "httpx", HttpxSyncHarness)
    _register(_async_registry, "httpx", HttpxAsyncHarness)
