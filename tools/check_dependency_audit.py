# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Runtime-dependency audit for the SDK workspace.

The dependency budget is a hard architectural constraint, not a preference:

- ``dexpace-sdk-core`` ships against the standard library plus ``furl`` and
  nothing else. ``furl`` (with its own transitive ``orderedmultidict`` / ``six``)
  is the one sanctioned third-party runtime dependency; it powers ``Url`` and is
  confined to that module by the import-linter contract.
- Every transport adapter depends on ``dexpace-sdk-core`` plus at most one
  third-party HTTP library (``aiohttp`` / ``httpx`` / ``requests``); the stdlib
  transport adds none.

This module reads each distribution's declared ``[project].dependencies`` from
its ``pyproject.toml`` with the standard-library ``tomllib`` parser and reports
any package that exceeds its budget. It inspects *declared* dependencies rather
than a resolved environment so the check is deterministic and needs no network.

Run as a script to audit the workspace:

    python tools/check_dependency_audit.py
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# The core distribution and its single permitted third-party runtime dependency.
_CORE = "dexpace-sdk-core"
_CORE_ALLOWED: frozenset[str] = frozenset({"furl"})

# Adapter distributions and the sibling packages that count as "internal" (free)
# dependencies rather than third-party ones.
_ADAPTERS: tuple[str, ...] = (
    "dexpace-sdk-http-stdlib",
    "dexpace-sdk-http-httpx",
    "dexpace-sdk-http-aiohttp",
    "dexpace-sdk-http-requests",
)
_INTERNAL_PREFIX = "dexpace-sdk-"
_MAX_ADAPTER_THIRD_PARTY = 1


@dataclass(frozen=True)
class Violation:
    """A distribution whose declared runtime dependencies break the budget."""

    distribution: str
    message: str


def repo_root() -> Path:
    """Return the workspace root (the directory that holds ``packages/``)."""
    return Path(__file__).resolve().parent.parent


def _normalise(requirement: str) -> str:
    """Return the bare, canonical distribution name from a requirement string.

    Strips version specifiers, extras, environment markers and surrounding
    whitespace, then lower-cases and normalises separators per PEP 503, so
    ``furl>=2.1.3`` and ``Furl`` both reduce to ``furl``.

    Args:
        requirement: A single ``[project].dependencies`` entry.

    Returns:
        The canonical (PEP 503) distribution name.
    """
    name = requirement.strip()
    for separator in (";", "[", "(", "@"):
        name = name.split(separator, 1)[0]
    for operator in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
        name = name.split(operator, 1)[0]
    return name.strip().lower().replace("_", "-").replace(".", "-")


def declared_dependencies(pyproject: Path) -> list[str]:
    """Return the canonical names of a distribution's runtime dependencies.

    Args:
        pyproject: Path to the distribution's ``pyproject.toml``.

    Returns:
        The canonicalised ``[project].dependencies`` names, in declared order.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    raw = project.get("dependencies", [])
    return [_normalise(entry) for entry in raw]


def _third_party(names: list[str]) -> set[str]:
    """Return declared names that are neither internal nor the workspace root."""
    return {name for name in names if not name.startswith(_INTERNAL_PREFIX)}


def _audit_core(names: list[str]) -> list[Violation]:
    """Audit the core distribution: stdlib + furl exactly, nothing more."""
    third_party = _third_party(names)
    if third_party == _CORE_ALLOWED:
        return []
    extra = sorted(third_party - _CORE_ALLOWED)
    missing = sorted(_CORE_ALLOWED - third_party)
    detail = []
    if extra:
        detail.append(f"unexpected third-party deps {extra}")
    if missing:
        detail.append(f"missing required deps {missing}")
    return [Violation(_CORE, "; ".join(detail))]


def _audit_adapter(distribution: str, names: list[str]) -> list[Violation]:
    """Audit one adapter: core plus at most one third-party library."""
    violations: list[Violation] = []
    if _CORE not in names:
        violations.append(Violation(distribution, f"does not depend on {_CORE}"))
    third_party = sorted(_third_party(names))
    if len(third_party) > _MAX_ADAPTER_THIRD_PARTY:
        violations.append(
            Violation(
                distribution,
                f"declares {len(third_party)} third-party deps {third_party}, "
                f"limit is {_MAX_ADAPTER_THIRD_PARTY}",
            )
        )
    return violations


def audit_workspace(root: Path | None = None) -> list[Violation]:
    """Audit every distribution's declared runtime dependency budget.

    Args:
        root: Workspace root to resolve against; defaults to the repository root.

    Returns:
        Every budget violation found; empty when the workspace is within budget.

    Raises:
        FileNotFoundError: If a distribution's ``pyproject.toml`` is missing.
    """
    base = (root or repo_root()).resolve()
    violations: list[Violation] = []
    core_pyproject = base / "packages" / _CORE / "pyproject.toml"
    violations.extend(_audit_core(declared_dependencies(core_pyproject)))
    for adapter in _ADAPTERS:
        pyproject = base / "packages" / adapter / "pyproject.toml"
        violations.extend(_audit_adapter(adapter, declared_dependencies(pyproject)))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: audit the workspace and report any budget violations.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when every distribution is within budget, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.parse_args(argv)
    violations = audit_workspace()
    if not violations:
        print("dependency audit: OK (core = stdlib + furl; adapters = core + <= 1 lib)")
        return 0
    print("dependency audit: FAILED", file=sys.stderr)
    for violation in violations:
        print(f"  - {violation.distribution}: {violation.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
