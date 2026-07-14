# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Registers the httpx harnesses into the shared transport-conformance battery.

``dexpace-sdk-core``'s ``tests/transport_conformance/conftest.py`` parametrizes
the ``sync_harness`` / ``async_harness`` fixtures over
``SYNC_HARNESS_FACTORIES`` / ``ASYNC_HARNESS_FACTORIES`` at collection time.
Appending this package's factories here — rather than editing that shared
module — keeps this certification task's changes confined to
``dexpace-sdk-http-httpx``, so it never collides with the equivalent
registration a sibling transport's certification task adds to its own
package.

Registration only has an effect when ``dexpace-sdk-core``'s own test tree is
part of the same pytest session (the normal case: the workspace's
``testpaths`` walks every package's ``tests/`` directory in one run, and all
five distributions share one editable-install virtualenv, so
``tests.transport_conformance.harness`` is the very same module object
regardless of which package's conftest imports it first). Running this
package's tests in isolation (``pytest packages/dexpace-sdk-http-httpx/tests``
on its own) never loads that module, so both the harness module and this
registration are best-effort: this package's own tests still run standalone,
just without the shared battery.
"""

from __future__ import annotations

_Registry = list[tuple[str, type]]

_registries: tuple[_Registry, _Registry] | None
try:
    from tests.transport_conformance.harness import (
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
