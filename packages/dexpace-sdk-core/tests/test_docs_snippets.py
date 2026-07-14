# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Keep the Markdown docs honest against the real public API.

Every fenced ``python`` block under ``docs/`` is checked two ways: it must parse
and compile (no stale syntax), and every ``dexpace`` import it names must resolve
to a real, current symbol. A snippet that imports a symbol the SDK no longer
exports fails here, so prose and code cannot drift apart silently.

Third-party and example-package imports (anything outside the ``dexpace``
namespace) are left to the reader's environment — only the SDK's own surface is
held to a hard resolve.
"""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS = _REPO_ROOT / "docs"

#: A fenced ``python`` code block; group 1 is the block's source.
_FENCE = re.compile(r"^```python\s*$(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def _iter_python_blocks() -> Iterator[tuple[str, str]]:
    """Yield ``(label, source)`` for every ``python`` block under ``docs/``.

    Yields:
        A stable ``file.md#pyN`` label and the block's source text, in document
        order across the sorted set of Markdown files.
    """
    for md in sorted(_DOCS.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        for index, match in enumerate(_FENCE.finditer(text)):
            yield f"{md.name}#py{index}", match.group(1)


_BLOCKS = list(_iter_python_blocks())


def _parse(source: str, label: str) -> ast.Module:
    """Parse a snippet, failing the test with its label on a syntax error.

    Args:
        source: The snippet source.
        label: The ``file.md#pyN`` label used in the failure message.

    Returns:
        The parsed module.
    """
    try:
        return ast.parse(source, filename=label)
    except SyntaxError as exc:
        pytest.fail(f"{label}: syntax error in doc snippet: {exc}")


def _assert_dexpace_imports_resolve(tree: ast.Module, label: str) -> None:
    """Assert every ``dexpace.*`` import in ``tree`` resolves to a real symbol.

    Args:
        tree: The parsed snippet.
        label: The ``file.md#pyN`` label used in failure messages.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            module = node.module
            strict = module.split(".", 1)[0] == "dexpace"
            try:
                imported = importlib.import_module(module)
            except ImportError:
                if strict:
                    pytest.fail(f"{label}: cannot import {module!r}")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                assert hasattr(imported, alias.name), (
                    f"{label}: {module}.{alias.name} is not a real symbol"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if module.split(".", 1)[0] != "dexpace":
                    continue
                try:
                    importlib.import_module(module)
                except ImportError:
                    pytest.fail(f"{label}: cannot import {module!r}")


def test_docs_contain_python_snippets() -> None:
    """The docs carry python snippets — a guard against a broken docs path."""
    assert _BLOCKS, f"no python snippets found under {_DOCS} — check the docs path"


@pytest.mark.parametrize(
    ("label", "source"),
    _BLOCKS,
    ids=[label for label, _ in _BLOCKS],
)
def test_doc_snippet_compiles_and_imports_resolve(label: str, source: str) -> None:
    """Each doc snippet compiles, and every ``dexpace`` import in it resolves."""
    tree = _parse(source, label)
    compile(tree, label, "exec")
    _assert_dexpace_imports_resolve(tree, label)
