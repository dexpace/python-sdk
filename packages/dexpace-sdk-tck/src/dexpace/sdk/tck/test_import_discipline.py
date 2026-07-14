# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Enforce that the TCK touches no underscore-prefixed (private) core module.

The kit is contractually built against core's PUBLIC re-exported surface only:
it must never reach into a ``dexpace.sdk.core`` module (or import a name) whose
dotted path carries an underscore-prefixed component. That doubles as an
underscore-discipline check on core itself — if the battery genuinely needed a
private module, the symbol would have to be promoted to the public surface
rather than reached into from here.

This test AST-scans every ``.py`` file shipped in the ``dexpace.sdk.tck``
package (static parse, no execution) and asserts the import graph names no
private core module or private core name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dexpace.sdk.tck

_CORE_PREFIX = "dexpace.sdk.core"


def _package_root() -> Path:
    """Return the on-disk directory of the installed ``dexpace.sdk.tck`` package."""
    origin = dexpace.sdk.tck.__file__
    assert origin is not None
    return Path(origin).resolve().parent


def _has_private_component(dotted: str) -> bool:
    """Return whether any component of ``dotted`` is underscore-prefixed.

    Dunder components (``__init__`` and friends) are not counted — no core
    module is a dunder — so only genuine single-underscore privates flag.
    """
    return any(part.startswith("_") and not part.startswith("__") for part in dotted.split("."))


def _private_core_imports(tree: ast.Module) -> list[str]:
    """Collect every private-core import target named in one module AST.

    Flags both ``import dexpace.sdk.core.x._y`` (a private module path) and
    ``from dexpace.sdk.core.x import _y`` (a private module or private name);
    relative imports (which target the TCK package itself) are ignored.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name
                for alias in node.names
                if alias.name.startswith(_CORE_PREFIX) and _has_private_component(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if node.level != 0 or module is None or not module.startswith(_CORE_PREFIX):
                continue
            if _has_private_component(module):
                offenders.append(module)
            offenders.extend(
                f"{module}.{alias.name}"
                for alias in node.names
                if alias.name.startswith("_") and not alias.name.startswith("__")
            )
    return offenders


def test_tck_imports_no_private_core_module() -> None:
    root = _package_root()
    violations: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = _private_core_imports(tree)
        if offenders:
            violations[str(path.relative_to(root))] = offenders
    assert not violations, f"TCK imports private core module(s): {violations}"
