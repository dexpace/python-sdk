# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the workspace packaging gates under ``tools/``.

Each checker is exercised two ways: it must stay green against the real
repository tree, and it must flag a violation seeded into a temporary
directory. The wheel smoke check is validated against the editable workspace
install (which mirrors the two-distribution namespace merge a built wheel pair
must also satisfy).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TOOLS_DIR = _REPO_ROOT / "tools"

HEADER = (
    "# Copyright (c) 2026 dexpace and Omar Aljarrah.\n"
    "# Licensed under the MIT License. See LICENSE.md in the repository root for details.\n"
)

NAMESPACE_PREFIXES = (
    "dexpace/__init__.py",
    "dexpace/sdk/__init__.py",
    "dexpace/sdk/http/__init__.py",
)


def _load_tool(name: str) -> ModuleType:
    """Import a ``tools/*.py`` module by file path.

    The ``tools`` directory is not an importable package, so load each script
    directly instead of relying on ``sys.path``.

    Args:
        name: Module basename without the ``.py`` suffix.

    Returns:
        The loaded module object.
    """
    spec = importlib.util.spec_from_file_location(name, _TOOLS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


namespace_check = _load_tool("check_namespace_packages")
header_check = _load_tool("check_mit_headers")
smoke_check = _load_tool("smoke_wheel_import")


def _write(path: Path, text: str) -> None:
    """Create ``path`` (and parents) with the given text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Namespace checker
# --------------------------------------------------------------------------- #


def test_real_repo_has_no_namespace_violations() -> None:
    assert namespace_check.find_namespace_violations(_REPO_ROOT) == []


@pytest.mark.parametrize("relpath", NAMESPACE_PREFIXES)
def test_stray_init_at_each_prefix_is_flagged(tmp_path: Path, relpath: str) -> None:
    offender = tmp_path / "packages" / "pkg-a" / "src" / relpath
    _write(offender, "")

    violations = namespace_check.find_namespace_violations(tmp_path)

    assert violations == [offender]


def test_clean_namespace_tree_passes(tmp_path: Path) -> None:
    # A leaf __init__.py is fine; only the three shared prefixes are forbidden.
    _write(tmp_path / "packages/pkg-a/src/dexpace/sdk/http/stdlib/__init__.py", HEADER)

    assert namespace_check.find_namespace_violations(tmp_path) == []


def test_namespace_main_reports_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(namespace_check, "repo_root", lambda: tmp_path)
    assert namespace_check.main() == 0

    _write(tmp_path / "packages/pkg-a/src/dexpace/sdk/__init__.py", "")
    assert namespace_check.main() == 1


# --------------------------------------------------------------------------- #
# MIT header checker
# --------------------------------------------------------------------------- #


@pytest.mark.req("NFR-13")
def test_real_repo_headers_present() -> None:
    assert header_check.find_header_violations(_REPO_ROOT) == []


def test_file_with_header_passes(tmp_path: Path) -> None:
    good = tmp_path / "mod.py"
    _write(good, HEADER + '\n"""Docstring."""\n')

    assert header_check.file_has_header(good) is True
    assert header_check.find_header_violations(tmp_path) == []


@pytest.mark.req("NFR-13")
def test_file_missing_header_is_flagged(tmp_path: Path) -> None:
    bad = tmp_path / "mod.py"
    _write(bad, '"""No licence header here."""\n')

    assert header_check.file_has_header(bad) is False
    assert header_check.find_header_violations(tmp_path) == [bad]


def test_shebang_before_header_is_allowed(tmp_path: Path) -> None:
    script = tmp_path / "script.py"
    _write(script, "#!/usr/bin/env python3\n" + HEADER)

    assert header_check.file_has_header(script) is True


@pytest.mark.req("NFR-13")
def test_wrong_second_line_is_flagged(tmp_path: Path) -> None:
    bad = tmp_path / "mod.py"
    _write(bad, "# Copyright (c) 2026 dexpace and Omar Aljarrah.\n# Wrong second line\n")

    assert header_check.file_has_header(bad) is False


def test_iter_python_files_skips_caches_and_dotdirs(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "mod.py", HEADER)
    _write(tmp_path / ".git" / "hook.py", HEADER)
    _write(tmp_path / "__pycache__" / "cached.py", HEADER)
    _write(tmp_path / "build" / "out.py", HEADER)
    _write(tmp_path / "thing.egg-info" / "meta.py", HEADER)

    found = set(header_check.iter_python_files(tmp_path))

    assert found == {tmp_path / "pkg" / "mod.py"}


def test_header_main_reports_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(header_check, "repo_root", lambda: tmp_path)
    _write(tmp_path / "ok.py", HEADER)
    assert header_check.main() == 0

    _write(tmp_path / "bad.py", '"""missing header."""\n')
    assert header_check.main() == 1


# --------------------------------------------------------------------------- #
# Wheel smoke check
# --------------------------------------------------------------------------- #


def test_smoke_check_passes_under_workspace_install() -> None:
    # The editable workspace resolves core and the stdlib adapter to two
    # distinct locations under a shared dexpace.sdk namespace -- the same
    # invariant a built wheel pair must satisfy (NFR-10).
    assert smoke_check.smoke_check() == []
