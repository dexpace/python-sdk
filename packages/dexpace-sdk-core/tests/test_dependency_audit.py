# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Runtime-dependency budget gate.

``tools/check_dependency_audit.py`` enforces the workspace's dependency budget:
``dexpace-sdk-core`` declares stdlib plus ``furl`` and nothing else, and every
transport adapter declares ``dexpace-sdk-core`` plus at most one third-party
library. These tests prove the live workspace is within budget and that the
audit rejects each way the budget can be broken.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _REPO_ROOT / "tools" / "check_dependency_audit.py"


def _load_tool() -> ModuleType:
    """Import ``tools/check_dependency_audit.py`` by file path."""
    spec = importlib.util.spec_from_file_location("_check_dependency_audit", _TOOL_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load dependency-audit tool from {_TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing so the tool's dataclasses can resolve their
    # (stringised) annotations against their own module namespace.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    """Provide the loaded dependency-audit tool once per module."""
    return _load_tool()


def _write_workspace(root: Path, deps: dict[str, list[str]]) -> None:
    """Materialise a minimal workspace of pyproject files under ``root``.

    Args:
        root: Directory to build the ``packages/`` tree under.
        deps: Distribution name -> its ``[project].dependencies`` list.
    """
    for distribution, dependencies in deps.items():
        package_dir = root / "packages" / distribution
        package_dir.mkdir(parents=True, exist_ok=True)
        rendered = ", ".join(f'"{dep}"' for dep in dependencies)
        (package_dir / "pyproject.toml").write_text(
            f'[project]\nname = "{distribution}"\ndependencies = [{rendered}]\n',
            encoding="utf-8",
        )


_WITHIN_BUDGET: dict[str, list[str]] = {
    "dexpace-sdk-core": ["furl>=2.1.3"],
    "dexpace-sdk-http-stdlib": ["dexpace-sdk-core>=0.1,<0.2"],
    "dexpace-sdk-http-httpx": ["dexpace-sdk-core>=0.1,<0.2", "httpx>=0.27,<0.29"],
    "dexpace-sdk-http-aiohttp": ["dexpace-sdk-core>=0.1,<0.2", "aiohttp>=3.9,<4.0"],
    "dexpace-sdk-http-requests": ["dexpace-sdk-core>=0.1,<0.2", "requests>=2.32,<3.0"],
}


@pytest.mark.req("NFR-1", "SEAM-1", "SEAM-2")
def test_live_workspace_is_within_budget(tool: ModuleType) -> None:
    violations = tool.audit_workspace(_REPO_ROOT)
    assert violations == [], [f"{v.distribution}: {v.message}" for v in violations]


@pytest.mark.req("NFR-2")
def test_synthetic_budget_workspace_passes(tool: ModuleType, tmp_path: Path) -> None:
    _write_workspace(tmp_path, _WITHIN_BUDGET)
    assert tool.audit_workspace(tmp_path) == []


@pytest.mark.req("NFR-1", "SEAM-1")
def test_core_with_extra_dependency_is_rejected(tool: ModuleType, tmp_path: Path) -> None:
    deps = dict(_WITHIN_BUDGET)
    deps["dexpace-sdk-core"] = ["furl>=2.1.3", "requests>=2.32"]
    _write_workspace(tmp_path, deps)
    violations = tool.audit_workspace(tmp_path)
    assert any(v.distribution == "dexpace-sdk-core" and "requests" in v.message for v in violations)


@pytest.mark.req("NFR-1")
def test_core_without_furl_is_rejected(tool: ModuleType, tmp_path: Path) -> None:
    deps = dict(_WITHIN_BUDGET)
    deps["dexpace-sdk-core"] = []
    _write_workspace(tmp_path, deps)
    violations = tool.audit_workspace(tmp_path)
    assert any(v.distribution == "dexpace-sdk-core" for v in violations)


@pytest.mark.req("NFR-2")
def test_adapter_with_two_third_party_libs_is_rejected(tool: ModuleType, tmp_path: Path) -> None:
    deps = dict(_WITHIN_BUDGET)
    deps["dexpace-sdk-http-httpx"] = ["dexpace-sdk-core>=0.1,<0.2", "httpx>=0.27", "anyio>=4.0"]
    _write_workspace(tmp_path, deps)
    violations = tool.audit_workspace(tmp_path)
    assert any(
        v.distribution == "dexpace-sdk-http-httpx" and "limit is 1" in v.message for v in violations
    )


@pytest.mark.req("NFR-2")
def test_adapter_without_core_is_rejected(tool: ModuleType, tmp_path: Path) -> None:
    deps = dict(_WITHIN_BUDGET)
    deps["dexpace-sdk-http-requests"] = ["requests>=2.32,<3.0"]
    _write_workspace(tmp_path, deps)
    violations = tool.audit_workspace(tmp_path)
    assert any(
        v.distribution == "dexpace-sdk-http-requests" and "does not depend on" in v.message
        for v in violations
    )
