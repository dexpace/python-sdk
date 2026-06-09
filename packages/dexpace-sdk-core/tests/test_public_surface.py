# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Public-API surface baseline test (Q1).

The project convention is a narrow, deliberate public API — an accurate
``__all__`` per package and stable Protocol/class signatures. Nothing
mechanically caught an accidental leak (a private helper sneaking into
``__all__``, a Protocol method signature changing, a ``with_*`` helper
disappearing) until this test.

The live surface is rebuilt by static analysis (no imports, no execution) via
``tools/surface_snapshot.py`` and compared against the committed baseline at
``tools/surface_baseline.json``. Any unexpected change fails the build.

Regenerating the baseline (only after an *intentional* public-API change, and
review the diff before committing it):

    python tools/surface_snapshot.py --write

A second test guards the baseline itself: it must be valid JSON whose top-level
keys are exactly the five distributions, so a corrupt or truncated baseline can
never silently disable the gate.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

# ``tools`` is not an installed package, so load the snapshot module by path.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _REPO_ROOT / "tools" / "surface_snapshot.py"
_BASELINE_PATH = _REPO_ROOT / "tools" / "surface_baseline.json"

_EXPECTED_DISTRIBUTIONS = frozenset(
    {
        "dexpace-sdk-core",
        "dexpace-sdk-http-stdlib",
        "dexpace-sdk-http-httpx",
        "dexpace-sdk-http-aiohttp",
        "dexpace-sdk-http-requests",
    }
)


def _load_snapshot_tool() -> ModuleType:
    """Import ``tools/surface_snapshot.py`` by file path.

    Returns:
        The loaded ``surface_snapshot`` module.

    Raises:
        ImportError: If the module spec cannot be created from the tool path.
    """
    spec = importlib.util.spec_from_file_location("_surface_snapshot", _TOOL_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load surface snapshot tool from {_TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def snapshot_tool() -> ModuleType:
    """Provide the loaded ``surface_snapshot`` tool module once per module."""
    return _load_snapshot_tool()


def _load_baseline() -> dict[str, dict[str, object]]:
    """Read and parse the committed baseline JSON."""
    parsed: dict[str, dict[str, object]] = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    return parsed


def test_live_surface_matches_committed_baseline(snapshot_tool: ModuleType) -> None:
    live = snapshot_tool.build_surface(_REPO_ROOT)
    baseline = _load_baseline()
    assert live == baseline, (
        "Public API surface drifted from the committed baseline. If this change "
        "is intentional, review the diff and regenerate with: "
        "python tools/surface_snapshot.py --write"
    )


def test_baseline_is_canonical_and_round_trips(snapshot_tool: ModuleType) -> None:
    # The committed file must match the tool's own canonical rendering exactly,
    # so a hand-edited or non-canonical baseline (different key order, missing
    # trailing newline) is rejected rather than silently accepted.
    baseline = _load_baseline()
    rendered = snapshot_tool.render(baseline)
    on_disk = _BASELINE_PATH.read_text(encoding="utf-8")
    assert rendered == on_disk, (
        "Baseline JSON is not in canonical form. Regenerate it with: "
        "python tools/surface_snapshot.py --write"
    )


def test_baseline_covers_exactly_the_five_distributions() -> None:
    baseline = _load_baseline()
    assert set(baseline) == _EXPECTED_DISTRIBUTIONS


def test_every_distribution_has_exports_and_definitions() -> None:
    baseline = _load_baseline()
    for dist, surface in baseline.items():
        assert "exports" in surface, f"{dist} baseline is missing the exports section"
        assert "definitions" in surface, f"{dist} baseline is missing the definitions section"
        assert surface["definitions"], f"{dist} baseline has no public definitions"


def test_core_init_packages_declare_all(snapshot_tool: ModuleType) -> None:
    # Every re-exporting subpackage of core must declare a static ``__all__``;
    # the snapshot tool only records packages that do, so a populated exports
    # map for core is the signal that the convention is being followed.
    live = snapshot_tool.build_surface(_REPO_ROOT)
    core_exports = live["dexpace-sdk-core"]["exports"]
    assert "dexpace.sdk.core.http.request" in core_exports
    assert "RequestBody" in core_exports["dexpace.sdk.core.http.request"]
