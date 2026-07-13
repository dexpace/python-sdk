# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Enforce the MIT licence header on every Python source file.

The project is MIT-licensed and every ``.py`` file -- source, tests, and
tooling alike -- must open with the two-line header before anything else
(an optional shebang may precede it). This module is the blocking gate that
scans the tree and fails when a file is missing or mangles the header.

Run it directly to check the current tree:

    uv run python tools/check_mit_headers.py

Exit code is ``0`` when every scanned file carries the header and ``1`` when
any file is missing it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

__all__ = [
    "EXPECTED_HEADER",
    "SKIP_DIR_NAMES",
    "file_has_header",
    "find_header_violations",
    "iter_python_files",
    "main",
    "repo_root",
]

# The exact two lines every .py file must begin with.
EXPECTED_HEADER: tuple[str, str] = (
    "# Copyright (c) 2026 dexpace and Omar Aljarrah.",
    "# Licensed under the MIT License. See LICENSE.md in the repository root for details.",
)

# Directory names pruned during the walk: build/cache artefacts and any
# dot-directory (``.git``, ``.venv``, ``.mypy_cache``, session scratch, ...).
# Excluding these keeps the scan to real, tracked source.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)


def repo_root() -> Path:
    """Return the repository root (the parent of this ``tools`` directory)."""
    return Path(__file__).resolve().parent.parent


def _is_skipped_dir(name: str) -> bool:
    """Return whether a directory name should be pruned from the walk."""
    return name.startswith(".") or name in SKIP_DIR_NAMES or name.endswith(".egg-info")


def iter_python_files(root: Path) -> Iterator[Path]:
    """Yield every ``.py`` file under a repo tree, skipping non-source dirs.

    Args:
        root: Directory to walk.

    Yields:
        Absolute paths to ``.py`` files, in sorted order per directory.
    """
    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = sorted(d for d in dirnames if not _is_skipped_dir(d))
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield dirpath / filename


def file_has_header(path: Path) -> bool:
    """Return whether a file opens with the expected MIT header.

    A single leading shebang line (``#!...``) is tolerated before the header;
    the two header lines must then appear back to back and match exactly.

    Args:
        path: Path to the ``.py`` file to inspect.

    Returns:
        ``True`` if the header is present and exact, ``False`` otherwise.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    start = 1 if lines and lines[0].startswith("#!") else 0
    return tuple(lines[start : start + 2]) == EXPECTED_HEADER


def find_header_violations(root: Path) -> list[Path]:
    """Find ``.py`` files under a repo tree missing the MIT header.

    Args:
        root: Repository root to scan.

    Returns:
        Sorted list of offending paths. Empty when every file is compliant.
    """
    return [path for path in iter_python_files(root) if not file_has_header(path)]


def main() -> int:
    """Report header violations and return a process exit code.

    Returns:
        ``0`` when every scanned file carries the header, ``1`` otherwise.
    """
    root = repo_root()
    violations = find_header_violations(root)
    if not violations:
        print("MIT header check: OK (every .py file carries the licence header)")
        return 0
    print("MIT header check: FAILED -- missing or malformed MIT header:")
    for path in violations:
        print(f"  {path.relative_to(root)}")
    print(
        "\nEvery .py file (src and tests) must start with these two lines:\n"
        f"  {EXPECTED_HEADER[0]}\n"
        f"  {EXPECTED_HEADER[1]}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
