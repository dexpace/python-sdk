# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Guard the PEP 420 namespace layout shared by every SDK distribution.

The five workspace packages share the ``dexpace.sdk.*`` prefix through implicit
namespace packages. A single stray ``__init__.py`` at ``src/dexpace/``,
``src/dexpace/sdk/``, or ``src/dexpace/sdk/http/`` in *any* package turns that
directory into a regular package and silently breaks namespace merging for the
other distributions -- and only under certain install orders, which makes the
failure maddening to reproduce. This module is the mechanical gate that keeps
the invariant honest instead of relying on reviewer memory.

Run it directly to check the current tree:

    uv run python tools/check_namespace_packages.py

Exit code is ``0`` when the layout is clean and ``1`` when any forbidden
``__init__.py`` is found.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "FORBIDDEN_NAMESPACE_INITS",
    "find_namespace_violations",
    "main",
    "repo_root",
]

# Relative to a package's ``src`` directory, these are the shared namespace
# prefixes that must remain implicit (no ``__init__.py``).
FORBIDDEN_NAMESPACE_INITS: tuple[str, ...] = (
    "dexpace/__init__.py",
    "dexpace/sdk/__init__.py",
    "dexpace/sdk/http/__init__.py",
)


def repo_root() -> Path:
    """Return the repository root (the parent of this ``tools`` directory)."""
    return Path(__file__).resolve().parent.parent


def find_namespace_violations(root: Path) -> list[Path]:
    """Find forbidden namespace ``__init__.py`` files under a repo tree.

    Scans every ``packages/*/src`` directory for an ``__init__.py`` at any of
    the shared namespace prefixes.

    Args:
        root: Repository root to scan. Must contain a ``packages`` directory.

    Returns:
        Sorted list of offending ``__init__.py`` paths. Empty when the layout
        is clean.
    """
    violations: list[Path] = []
    for src_dir in sorted(root.glob("packages/*/src")):
        for relpath in FORBIDDEN_NAMESPACE_INITS:
            candidate = src_dir / relpath
            if candidate.is_file():
                violations.append(candidate)
    return violations


def main() -> int:
    """Report namespace violations and return a process exit code.

    Returns:
        ``0`` when the namespace layout is clean, ``1`` otherwise.
    """
    root = repo_root()
    violations = find_namespace_violations(root)
    if not violations:
        print("namespace check: OK (no __init__.py at shared prefixes)")
        return 0
    print("namespace check: FAILED -- forbidden __init__.py breaks PEP 420 merging:")
    for path in violations:
        print(f"  {path.relative_to(root)}")
    print(
        "\nRemove these files. The dexpace/, dexpace/sdk/, and dexpace/sdk/http/\n"
        "directories must stay implicit namespace packages so every distribution\n"
        "can share the dexpace.sdk.* prefix."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
