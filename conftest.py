"""Workspace-root conftest for the dexpace SDK test suite.

Sole purpose right now: own and close an event loop on behalf of
``pytest-asyncio``'s ``_temporary_event_loop_policy`` fixture (plugin.py:618),
which calls ``asyncio.get_event_loop()`` to capture the "old" loop, gets a
freshly-created one back when no loop is current, and never closes it. The
leftover loop (and the socket pair backing its self-pipe) escalates as a
``PytestUnraisableExceptionWarning`` under our ``filterwarnings = ["error"]``
gate.

Pre-creating and registering an event loop here means that capture call
finds *our* loop instead of conjuring a new one, and the session finalizer
closes it deterministically.

Tracked upstream in pytest-asyncio; this conftest can go away once a fix
ships.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def _own_default_event_loop() -> Iterator[None]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        # ``pytest_asyncio`` may have replaced the current loop; close
        # whichever loop the policy currently points at.
        try:
            current = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            current = None
        for candidate in {loop, current}:
            if candidate is not None and not candidate.is_closed():
                candidate.close()
        asyncio.set_event_loop(None)
