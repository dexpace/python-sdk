# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Import-linter contract gate.

The checked-in ``.importlinter`` contracts encode the SDK's dependency-direction
invariants: adapters depend on core (never the reverse), ``furl`` is confined to
the single ``Url`` module, and the pipeline runners sit above the pure algorithm
layer. ``lint-imports`` enforces them in CI; these tests prove the gate is wired
correctly — it passes against the real tree, and a deliberately seeded violation
(a module importing ``furl`` outside the ``Url`` module) turns it red.

The contracts are exercised through the real ``lint-imports`` console script,
exactly as CI runs it, so the test cannot drift from the shipped behaviour.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from importlinter.api import read_configuration

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _REPO_ROOT / ".importlinter"
_EXPECTED_CONTRACTS = {
    "Adapters depend on core, never the reverse",
    "furl is confined to the Url module",
    "Pipeline runners sit above the pure algorithm layer",
}


def _lint_imports_executable() -> str:
    """Return the path to the ``lint-imports`` console script in this env."""
    candidate = Path(sys.executable).parent / "lint-imports"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("lint-imports")
    if found is not None:
        return found
    raise FileNotFoundError("lint-imports console script not found in this environment")


def _run_lint_imports() -> subprocess.CompletedProcess[str]:
    """Run ``lint-imports`` from the repo root, capturing its output."""
    return subprocess.run(
        [_lint_imports_executable()],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_config_declares_the_expected_contracts() -> None:
    configuration = read_configuration(str(_CONFIG_PATH))
    names = {contract["name"] for contract in configuration["contracts_options"]}
    assert names == _EXPECTED_CONTRACTS


def test_real_tree_satisfies_all_contracts() -> None:
    result = _run_lint_imports()
    assert result.returncode == 0, (
        "lint-imports failed against the real tree:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Contracts: 3 kept, 0 broken." in result.stdout


def test_seeded_furl_violation_breaks_the_build() -> None:
    # A module importing furl anywhere but the Url implementation must break the
    # furl-confinement contract. Seed one directly in the source tree, confirm
    # the gate goes red, and always remove it again.
    seed = _REPO_ROOT / "packages" / "dexpace-sdk-core" / "src" / "dexpace" / "sdk" / "core"
    seed = seed / "_furl_confinement_seed.py"
    seed.write_text(
        "# Copyright (c) 2026 dexpace and Omar Aljarrah.\n"
        "# Licensed under the MIT License. See LICENSE.md in the repository root for details.\n"
        "from __future__ import annotations\n"
        "import furl  # noqa: F401\n",
        encoding="utf-8",
    )
    try:
        result = _run_lint_imports()
    finally:
        seed.unlink()
    assert result.returncode != 0, "seeded furl import did not break the build"
    assert "furl is confined to the Url module BROKEN" in result.stdout
    assert "is not allowed to import furl" in result.stdout


@pytest.mark.parametrize("config_key", ["root_packages"])
def test_config_scopes_all_five_distributions(config_key: str) -> None:
    configuration = read_configuration(str(_CONFIG_PATH))
    roots = configuration["session_options"][config_key]
    assert "dexpace.sdk.core" in roots
    assert sum(1 for name in roots if name.startswith("dexpace.sdk.http.")) == 4
