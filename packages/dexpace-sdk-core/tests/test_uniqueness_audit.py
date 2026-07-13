# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Single-source (uniqueness) audit gate.

``tools/check_uniqueness.py`` enforces that every algorithm registered as
single-sourced is defined in exactly one module and imported everywhere else —
the guard that stops a hand-maintained async twin from copy-pasting a formula
instead of importing it.

These tests prove two things: the live tree passes the audit (every registered
formula is unique), and the mechanism actually catches duplication. Because the
pure algorithm modules land in later milestones, the detection test drives the
audit against a generated fixture pair — one module defining a backoff formula,
one restating its body — and confirms the restatement is flagged while a module
that imports the formula is not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _REPO_ROOT / "tools" / "check_uniqueness.py"

_FORMULA_BODY = (
    "def backoff_delay(attempt: int, base: float, cap: float) -> float:\n"
    "    delay = base * (2**attempt)\n"
    "    jitter = delay * 0.5\n"
    "    return min(cap, delay + jitter)\n"
)
_RESTATED_BODY = (
    "def recompute_backoff(attempt: int, base: float, cap: float) -> float:\n"
    "    delay = base * (2**attempt)\n"
    "    jitter = delay * 0.5\n"
    "    return min(cap, delay + jitter)\n"
)
_IMPORTED_BODY = (
    "from formula_source import backoff_delay\n\n\n"
    "def next_delay(attempt: int) -> float:\n"
    "    return backoff_delay(attempt, base=0.5, cap=30.0)\n"
)


def _load_tool() -> ModuleType:
    """Import ``tools/check_uniqueness.py`` by file path."""
    spec = importlib.util.spec_from_file_location("_check_uniqueness", _TOOL_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load uniqueness tool from {_TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing so the tool's dataclasses can resolve their
    # (stringised) annotations against their own module namespace.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    """Provide the loaded uniqueness-audit tool once per module."""
    return _load_tool()


def test_live_tree_has_no_restated_algorithms(tool: ModuleType) -> None:
    duplications = tool.find_restated_algorithms(tool.source_roots(_REPO_ROOT), tool.SINGLE_SOURCED)
    assert duplications == [], [tool.format_duplication(dup) for dup in duplications]


def test_restated_formula_is_flagged(tool: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "formula_source.py").write_text(_FORMULA_BODY, encoding="utf-8")
    (tmp_path / "formula_restated.py").write_text(_RESTATED_BODY, encoding="utf-8")
    registry = (
        tool.Algorithm(
            module="formula_source",
            function="backoff_delay",
            description="Exponential backoff with jitter.",
        ),
    )
    duplications = tool.find_restated_algorithms([tmp_path], registry)
    assert len(duplications) == 1
    assert duplications[0].restated_in.module == "formula_restated"
    assert duplications[0].restated_in.name == "recompute_backoff"


def test_imported_formula_is_not_flagged(tool: ModuleType, tmp_path: Path) -> None:
    # The accepted pattern: define once, import elsewhere. The importing module
    # has no duplicate body, so the audit stays green.
    (tmp_path / "formula_source.py").write_text(_FORMULA_BODY, encoding="utf-8")
    (tmp_path / "formula_imported.py").write_text(_IMPORTED_BODY, encoding="utf-8")
    registry = (
        tool.Algorithm(
            module="formula_source",
            function="backoff_delay",
            description="Exponential backoff with jitter.",
        ),
    )
    assert tool.find_restated_algorithms([tmp_path], registry) == []


def test_missing_registered_algorithm_raises(tool: ModuleType, tmp_path: Path) -> None:
    # A stale registry entry (canonical function absent) must fail loudly rather
    # than silently pass, so the audit can never rot into a no-op by accident.
    (tmp_path / "formula_source.py").write_text(_FORMULA_BODY, encoding="utf-8")
    registry = (
        tool.Algorithm(module="formula_source", function="does_not_exist", description="x"),
    )
    with pytest.raises(LookupError):
        tool.find_restated_algorithms([tmp_path], registry)
