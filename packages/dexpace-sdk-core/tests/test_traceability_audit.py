# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Tests for the requirement-ID traceability audit (tools/traceability_audit.py).

These prove the audit machinery end-to-end against its self-contained
placeholder index: an uncovered MUST in a flagged subsystem is a gap, adding a
covering marker closes it, the milestone-scoped enablement and the deviations
(N/A) seam both behave, and the matrix artifact renders deterministically. A
pair of subprocess runs verify the ``req`` marker collects warning-free under
``-W error::pytest.PytestUnknownMarkWarning`` while an unregistered marker still
escalates to an error there.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

# ``tools`` is not an installed package, so load the audit module by path — the
# same pattern the public-surface test uses.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _REPO_ROOT / "tools" / "traceability_audit.py"


def _load_audit_tool() -> ModuleType:
    """Import ``tools/traceability_audit.py`` by file path."""
    spec = importlib.util.spec_from_file_location("_traceability_audit_under_test", _TOOL_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load traceability audit tool from {_TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing so the module's dataclasses can resolve their
    # (stringified) annotations against their own namespace under Python 3.12+.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    """Provide the loaded audit tool module once per test module."""
    return _load_audit_tool()


# --- Core audit behaviour --------------------------------------------------


def test_audit_fails_on_uncovered_must_then_passes_when_covered(tool: ModuleType) -> None:
    index = tool.build_index([tool.Requirement("X-1", tool.Level.MUST, "demo", "desc")])

    uncovered = tool.audit(index, {}, enabled_subsystems=["demo"])
    assert not uncovered.ok
    assert [gap.requirement.id for gap in uncovered.gaps] == ["X-1"]

    covered = tool.audit(
        index, {"X-1": ["pkg/tests/test_demo.py::test_it"]}, enabled_subsystems=["demo"]
    )
    assert covered.ok
    assert covered.gaps == ()
    assert covered.matrix["X-1"] == ("pkg/tests/test_demo.py::test_it",)


def test_placeholder_index_gap_closes_when_all_must_covered(tool: ModuleType) -> None:
    index = tool.PLACEHOLDER_INDEX
    must_by_subsystem: dict[str, list[str]] = {}
    for req in index.requirements:
        if req.level is tool.Level.MUST:
            must_by_subsystem.setdefault(req.subsystem, []).append(req.id)
    subsystem, must_ids = next(iter(must_by_subsystem.items()))

    gap = tool.audit(index, {}, enabled_subsystems=[subsystem])
    assert not gap.ok
    assert {g.requirement.id for g in gap.gaps} == set(must_ids)

    collected = {rid: [f"pkg/tests/test_x.py::test_{rid}"] for rid in must_ids}
    assert tool.audit(index, collected, enabled_subsystems=[subsystem]).ok


def test_empty_enabled_subsystems_never_gaps(tool: ModuleType) -> None:
    # Milestone-scoped: with nothing flagged done the whole (placeholder) spec
    # is uncovered yet the audit fails on nothing.
    result = tool.audit(tool.PLACEHOLDER_INDEX, {})
    assert result.ok
    assert result.gaps == ()


def test_na_ids_exclude_requirements_from_gaps(tool: ModuleType) -> None:
    # ``retry`` MUST ids are RETRY-19 and RETRY-07; marking both N/A leaves no
    # gap even with the subsystem enabled.
    result = tool.audit(
        tool.PLACEHOLDER_INDEX,
        {},
        enabled_subsystems=["retry"],
        na_ids=["RETRY-19", "RETRY-07"],
    )
    assert result.ok
    assert result.na_ids == frozenset({"RETRY-19", "RETRY-07"})


def test_should_and_may_requirements_never_gap(tool: ModuleType) -> None:
    index = tool.build_index(
        [
            tool.Requirement("S-1", tool.Level.SHOULD, "demo", "d"),
            tool.Requirement("M-1", tool.Level.MAY, "demo", "d"),
        ]
    )
    assert tool.audit(index, {}, enabled_subsystems=["demo"]).ok


def test_unknown_marker_ids_are_surfaced_not_matrixed(tool: ModuleType) -> None:
    result = tool.audit(tool.PLACEHOLDER_INDEX, {"NOPE-1": ["t::a"]})
    assert result.unknown_marker_ids == ("NOPE-1",)
    assert "NOPE-1" not in result.matrix


def test_build_index_rejects_duplicate_ids(tool: ModuleType) -> None:
    with pytest.raises(ValueError, match="duplicate requirement id"):
        tool.build_index(
            [
                tool.Requirement("D-1", tool.Level.MUST, "x", "a"),
                tool.Requirement("D-1", tool.Level.MUST, "x", "b"),
            ]
        )


# --- Matrix artifact rendering ---------------------------------------------


def test_matrix_json_covers_every_requirement(tool: ModuleType) -> None:
    index = tool.PLACEHOLDER_INDEX
    result = tool.audit(index, {"RETRY-19": ["pkg/tests/test_r.py::test_ra"]})
    text = tool.render_matrix_json(result, index)

    assert text.endswith("\n")
    document = json.loads(text)
    assert document["schema"].startswith("dexpace-traceability-matrix/")
    assert len(document["requirements"]) == len(index.requirements)
    row = next(r for r in document["requirements"] if r["id"] == "RETRY-19")
    assert row["covered"] is True
    assert row["tests"] == ["pkg/tests/test_r.py::test_ra"]
    # Deterministic: re-rendering the same result is byte-for-byte identical.
    assert tool.render_matrix_json(result, index) == text


def test_matrix_csv_has_one_row_per_requirement(tool: ModuleType) -> None:
    index = tool.PLACEHOLDER_INDEX
    result = tool.audit(index, {"RETRY-19": ["a::b", "c::d"]})
    rows = list(csv.reader(io.StringIO(tool.render_matrix_csv(result, index))))

    assert rows[0][0] == "requirement_id"
    assert len(rows) == len(index.requirements) + 1
    retry_row = next(r for r in rows[1:] if r[0] == "RETRY-19")
    assert retry_row[3] == "True"  # covered
    assert retry_row[4] == "2"  # test_count
    assert retry_row[5] == "a::b;c::d"


# --- Deviations (N/A) seam -------------------------------------------------


def test_na_ids_from_missing_or_none_path_is_empty(tool: ModuleType, tmp_path: Path) -> None:
    assert tool.na_ids_from_file(None) == frozenset()
    assert tool.na_ids_from_file(tmp_path / "does-not-exist.md") == frozenset()


def test_na_ids_from_file_reads_ids_and_skips_comments(tool: ModuleType, tmp_path: Path) -> None:
    ledger = tmp_path / "deviations.txt"
    ledger.write_text("# comment\nRETRY-19\n\n  AUTH-02  \n", encoding="utf-8")
    assert tool.na_ids_from_file(ledger) == frozenset({"RETRY-19", "AUTH-02"})


# --- Plugin behaviour ------------------------------------------------------


class _FakeMark:
    def __init__(self, args: tuple[str, ...]) -> None:
        self.args = args


class _FakeItem:
    def __init__(self, nodeid: str, req_ids: list[str]) -> None:
        self.nodeid = nodeid
        self._marks = [_FakeMark((rid,)) for rid in req_ids]

    def iter_markers(self, name: str) -> Iterator[_FakeMark]:
        assert name == "req"
        return iter(self._marks)


def test_plugin_collects_markers_and_writes_matrix(tool: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "matrix.json"
    plugin = tool.TraceabilityPlugin(output_path=out)
    items = [
        _FakeItem("pkg/tests/test_a.py::test_one", ["RETRY-19"]),
        _FakeItem("pkg/tests/test_a.py::test_two", ["RETRY-19", "AUTH-02"]),
    ]
    plugin.pytest_collection_modifyitems(None, items)
    plugin.pytest_collection_finish(None)

    assert out.is_file()
    document = json.loads(out.read_text(encoding="utf-8"))
    retry = next(r for r in document["requirements"] if r["id"] == "RETRY-19")
    assert retry["tests"] == [
        "pkg/tests/test_a.py::test_one",
        "pkg/tests/test_a.py::test_two",
    ]
    auth = next(r for r in document["requirements"] if r["id"] == "AUTH-02")
    assert auth["tests"] == ["pkg/tests/test_a.py::test_two"]


def test_plugin_gate_fails_on_uncovered_must(tool: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "gated.json"
    plugin = tool.TraceabilityPlugin(output_path=out, enabled_subsystems=["retry"], gate=True)
    plugin.pytest_collection_modifyitems(None, [])
    with pytest.raises(pytest.UsageError, match="uncovered MUST"):
        plugin.pytest_collection_finish(None)
    assert out.is_file()  # artifact written even though the gate then raised


def test_build_plugin_from_env_honours_overrides(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "na.txt"
    ledger.write_text("RETRY-19\n", encoding="utf-8")
    out = tmp_path / "env-matrix.json"
    monkeypatch.setenv("DEXPACE_TRACEABILITY_MATRIX", str(out))
    monkeypatch.setenv("DEXPACE_TRACEABILITY_ENABLE", "retry, auth")
    monkeypatch.setenv("DEXPACE_TRACEABILITY_NA", str(ledger))
    monkeypatch.setenv("DEXPACE_TRACEABILITY_GATE", "yes")

    plugin = tool.build_plugin_from_env()
    plugin.pytest_collection_modifyitems(None, [])
    with pytest.raises(pytest.UsageError) as excinfo:
        plugin.pytest_collection_finish(None)

    message = str(excinfo.value)
    assert "RETRY-07" in message and "AUTH-02" in message
    assert "RETRY-19" not in message  # excluded via the N/A ledger
    assert out.is_file()


# --- Marker registration is warning-free -----------------------------------


def _registered_req_marker(tool: ModuleType) -> str:
    """Return the ``req`` marker entry from the root pyproject markers list."""
    pyproject = tool.repo_root() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    for entry in markers:
        if entry.startswith("req("):
            return entry
    raise AssertionError("no 'req' marker registered in root pyproject.toml")


def _run_pytest(project_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run pytest hermetically inside ``project_dir`` and capture the result."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )


def test_req_marker_is_registered_in_pyproject(tool: ModuleType) -> None:
    assert _registered_req_marker(tool).startswith("req(")


def test_registered_req_marker_collects_warning_free(tool: ModuleType, tmp_path: Path) -> None:
    marker = _registered_req_marker(tool)
    assert "'" not in marker  # embedded as a TOML literal string below
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = "-W error::pytest.PytestUnknownMarkWarning --strict-markers"\n'
        f"markers = ['{marker}']\n",
        encoding="utf-8",
    )
    (tmp_path / "test_registered.py").write_text(
        'import pytest\n\n\n@pytest.mark.req("RETRY-19")\ndef test_ok() -> None:\n    assert True\n',
        encoding="utf-8",
    )
    result = _run_pytest(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unregistered_marker_errors_under_w_error(tmp_path: Path) -> None:
    # Negative control: without registration, PytestUnknownMarkWarning fires and
    # ``-W error`` escalates it, proving the gate is live.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-W error::pytest.PytestUnknownMarkWarning"\n',
        encoding="utf-8",
    )
    (tmp_path / "test_unregistered.py").write_text(
        "import pytest\n\n\n@pytest.mark.totally_unregistered\ndef test_x() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    result = _run_pytest(tmp_path)
    assert result.returncode != 0
