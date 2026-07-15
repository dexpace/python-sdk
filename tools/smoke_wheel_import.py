# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Smoke-test built wheels for namespace merging and ``py.typed`` shipping.

Editable installs (``uv sync``) and built wheels resolve PEP 420 namespace
packages through different machinery, so a layout that works in the workspace
venv can still break once the wheels are installed side by side. This script
is meant to run inside a *fresh* venv that has only the built ``dexpace-sdk-core``
and one adapter wheel installed -- never the editable workspace venv.

It asserts that:

* ``dexpace.sdk.core`` and the adapter import together in one interpreter,
  proving the shared ``dexpace.sdk`` namespace merges across two wheels
  (NFR-10);
* the two distributions resolve to *different* installed locations (a real
  namespace merge, not one wheel shadowing the other); and
* each package ships its ``py.typed`` marker (PEP 561).

Run it, after installing the wheels, as:

    python tools/smoke_wheel_import.py

Exit code is ``0`` on success and ``1`` on the first failed assertion. Only the
standard library is imported here so the script runs in a minimal venv.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

__all__ = ["ADAPTER_MODULE", "CORE_MODULE", "main", "smoke_check"]

CORE_MODULE = "dexpace.sdk.core"
ADAPTER_MODULE = "dexpace.sdk.http.stdlib"


def _package_dir(module: ModuleType) -> Path:
    """Return the on-disk directory backing an imported package.

    Args:
        module: An imported package module.

    Returns:
        The directory that contains the package's ``__init__.py``.

    Raises:
        RuntimeError: If the module has no locatable file on disk.
    """
    origin = getattr(module, "__file__", None)
    if origin is None:
        raise RuntimeError(f"{module.__name__} has no __file__; cannot locate it on disk")
    return Path(origin).resolve().parent


def smoke_check() -> list[str]:
    """Import core plus the adapter and verify packaging invariants.

    Returns:
        A list of human-readable failure messages. Empty when every check
        passes.
    """
    failures: list[str] = []

    core = importlib.import_module(CORE_MODULE)
    adapter = importlib.import_module(ADAPTER_MODULE)

    # Importing a real public symbol from each proves the modules load, not
    # just that an empty namespace directory happens to resolve.
    importlib.import_module(f"{ADAPTER_MODULE}.urllib_http_client")

    core_dir = _package_dir(core)
    adapter_dir = _package_dir(adapter)

    if core_dir == adapter_dir:
        failures.append(
            f"expected core and adapter in different locations, both resolved to {core_dir}"
        )

    for label, module, package_dir in (
        ("core", core, core_dir),
        ("adapter", adapter, adapter_dir),
    ):
        marker = package_dir / "py.typed"
        if not marker.is_file():
            failures.append(f"{label} ({module.__name__}) is missing py.typed at {marker}")

    return failures


def main() -> int:
    """Run the wheel smoke check and return a process exit code.

    Returns:
        ``0`` when all checks pass, ``1`` otherwise.
    """
    failures = smoke_check()
    if not failures:
        print(f"wheel smoke check: OK ({CORE_MODULE} + {ADAPTER_MODULE} import cleanly)")
        return 0
    print("wheel smoke check: FAILED")
    for message in failures:
        print(f"  {message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
