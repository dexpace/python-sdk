# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

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
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


def _load_traceability_plugin() -> object:
    """Load and construct the requirement-traceability plugin.

    ``tools`` is not an installed package, so the audit module is imported by
    file path (the same pattern the public-surface test uses). The plugin
    collects ``@pytest.mark.req(...)`` markers and writes a matrix artifact; it
    only fails a run when explicitly gated via ``DEXPACE_TRACEABILITY_*`` env
    vars, so registering it here is inert for the normal suite.

    Returns:
        A configured traceability plugin instance.

    Raises:
        ImportError: If the audit module cannot be loaded from ``tools/``.
    """
    tool_path = Path(__file__).resolve().parent / "tools" / "traceability_audit.py"
    spec = importlib.util.spec_from_file_location("_dexpace_traceability_audit", tool_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load traceability audit tool from {tool_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing so the module's dataclasses can resolve their
    # (stringified) annotations against their own namespace under Python 3.12+.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.build_plugin_from_env()


def pytest_configure(config: pytest.Config) -> None:
    """Register the requirement-traceability plugin for the session."""
    config.pluginmanager.register(_load_traceability_plugin(), "dexpace-traceability")


@pytest.fixture(scope="session", autouse=True)
def _own_default_event_loop() -> Iterator[None]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        # ``pytest_asyncio`` may have replaced the current loop; close
        # whichever loop is current at teardown. ``asyncio.get_event_loop``
        # (not the policy API — deprecated in 3.14) returns the set loop
        # without warning on every supported version, and raises
        # ``RuntimeError`` on 3.14+ when none is set.
        try:
            current = asyncio.get_event_loop()
        except RuntimeError:
            current = None
        for candidate in {loop, current}:
            if candidate is not None and not candidate.is_closed():
                candidate.close()
        asyncio.set_event_loop(None)
