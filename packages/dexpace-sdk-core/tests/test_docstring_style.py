# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Docstring-style guardrail: no Sphinx cross-reference roles.

The project convention (CLAUDE.md) mandates Google-style docstrings with plain
backticks for code/type references — never Sphinx roles such as
``:class:`Foo```. Neither ``ruff`` nor ``mypy`` inspects docstring prose, so a
one-time conversion sweep would silently rot without a mechanical guard. This
test scans every package source and test file and fails if any role reappears,
keeping the convention enforceable rather than aspirational.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SELF = Path(__file__).resolve()

# Assembled from parts (and an explicit backtick via ``chr(96)``) so this file
# contains no literal Sphinx role and never flags itself.
_ROLE_NAMES = ("class", "meth", "func", "data", "attr", "mod", "exc", "obj", "paramref", "term")
_ROLE_RE = re.compile(r":(?:" + "|".join(_ROLE_NAMES) + r"):" + chr(96))


def _python_sources() -> list[Path]:
    """Return every ``.py`` file under each package's ``src`` and ``tests``."""
    files: list[Path] = []
    packages = _REPO_ROOT / "packages"
    for pkg in sorted(packages.iterdir()):
        for sub in ("src", "tests"):
            root = pkg / sub
            if root.is_dir():
                files.extend(p for p in root.rglob("*.py") if p.resolve() != _SELF)
    return files


def test_no_sphinx_roles_in_docstrings() -> None:
    """No source or test file may use a Sphinx cross-reference role."""
    offenders: list[str] = []
    for path in _python_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ROLE_RE.search(line):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Sphinx cross-reference roles are forbidden; use plain backticks "
        "(e.g. ':class:`Foo`' becomes '`Foo`'). Offenders:\n" + "\n".join(offenders)
    )


def test_guard_detects_a_role() -> None:
    """The matcher itself works (so a green run means real coverage)."""
    assert _ROLE_RE.search(":meth:" + chr(96) + "Foo.bar" + chr(96)) is not None
    assert _ROLE_RE.search("plain `Foo` reference") is None
