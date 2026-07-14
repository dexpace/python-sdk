# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Registers the real-``aiohttp`` harness into the shared conformance registry.

The shared transport-adapter conformance battery (built in PY-M8-01, at
``packages/dexpace-sdk-core/tests/transport_conformance/``) reads
``ASYNC_HARNESS_FACTORIES`` at collection time to parametrize its
``async_harness`` fixture. Appending this package's harness factory here is
the entire integration: no test body in that battery changes.

``packages/dexpace-sdk-core/tests`` is a plain (``__init__.py``-carrying)
package, not an installed distribution, so it is only importable as
``tests.transport_conformance...`` once ``packages/dexpace-sdk-core`` itself
is on ``sys.path``. Running the full workspace suite (``uv run pytest``, which
lists core's tests directory first in ``testpaths``) already puts it there
before this conftest loads. The explicit insertion below is a safety net so
this package's own test suite also resolves the import when run in isolation
(``pytest packages/dexpace-sdk-http-aiohttp/tests``), without depending on
collection order.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

_CORE_TESTS_ROOT = Path(__file__).resolve().parents[2] / "dexpace-sdk-core"
if str(_CORE_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_TESTS_ROOT))

from tests.transport_conformance.harness import ASYNC_HARNESS_FACTORIES  # noqa: E402

from conformance_harness import AiohttpAsyncHarness, reap_test_resources  # noqa: E402

if TYPE_CHECKING:
    import pytest


def _register(registry: list[tuple[str, type]], label: str, factory: type) -> None:
    """Append ``(label, factory)`` to ``registry`` unless the label is present."""
    if not any(existing == label for existing, _ in registry):
        registry.append((label, factory))


_register(ASYNC_HARNESS_FACTORIES, "aiohttp", AiohttpAsyncHarness)


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Close every real session/server `AiohttpAsyncHarness` built for ``item``.

    A *global* pytest hook (unlike a fixture, which is only visible within
    its own conftest.py's directory subtree) — this fires after every test in
    the whole run, including the shared battery's own ``test_async_contract.py``,
    which is what makes it possible to guarantee cleanup for a harness whose
    test bodies live in a different package entirely and never call
    `aclose_client` themselves. `AiohttpHttpClient` sessions and the loopback
    server behind them are real asyncio resources bound to the event loop
    pytest-asyncio just used to run ``item`` — that loop is no longer
    *running* by this point (`pytest_runtest_call` has already returned) but
    is not yet *closed* either, so driving the real async close here, on that
    same loop, is what actually releases the underlying OS sockets instead of
    leaving them for garbage collection to discover on an unpredictable
    schedule.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if not loop.is_closed():
        loop.run_until_complete(reap_test_resources())
