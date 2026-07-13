# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Single-source audit for algorithm formulas.

The project single-sources every non-trivial algorithm — the retry classifier,
the backoff calculator, the pacing parser, the redirect-resolution rules, and
so on. Each such formula is *defined in exactly one module* and *imported*
everywhere else. This matters most for the hand-maintained async policy twins,
which must call the shared formula rather than restate it: a copy-pasted body
drifts silently the first time the sync side is tuned and the async side is not.

This module provides the mechanism that catches that drift. It parses the
target trees with the standard-library ``ast`` parser (no imports, no
execution) and fingerprints every function body. For each algorithm registered
as single-sourced, it verifies that the canonical body appears in no other
module. Registration is opt-in: an algorithm joins ``SINGLE_SOURCED`` the moment
its module is written, and from then on any restatement of its body fails the
audit.

A whole-tree "flag every duplicated body" heuristic was deliberately rejected as
the gate: the sync/async twins legitimately share dozens of tiny identical
bodies (``__init__`` field stores, ``__hash__``, builder splices), so an
unregistered denylist would be noise. The registry keeps the audit precise —
zero false positives, and every entry documents an invariant on purpose.

The registry is empty today because the pure algorithm modules land in later
milestones (M4/M6). The companion pytest exercises the mechanism against a
fixture pair (one file defining a formula, one restating it) so the machinery is
proven to catch duplication before there is anything real to catch.

Run as a script to audit the live tree:

    python tools/check_uniqueness.py
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Algorithm:
    """A formula that must be defined in exactly one module.

    Attributes:
        module: Dotted module path of the canonical definition, relative to a
            scanned root (e.g. ``dexpace.sdk.core.pipeline._backoff``).
        function: Name of the canonical top-level function.
        description: Human-readable note on what the formula computes, shown in
            audit failures so the invariant is self-explanatory.
    """

    module: str
    function: str
    description: str


# Registry of single-sourced algorithms.
#
# EMPTY today — the pure algorithm modules (retry classifier, backoff
# calculator, pacing parser, query codec, SSE grammar, redirect resolution)
# are built in later milestones. Register each one here when it is written; the
# audit then forbids any other module (notably an async twin) from restating its
# body instead of importing it.
#
# Example — uncomment and adapt once the module exists:
#
#     Algorithm(
#         module="dexpace.sdk.core.pipeline._backoff",
#         function="compute_delay",
#         description="Exponential backoff with full jitter.",
#     ),
SINGLE_SOURCED: tuple[Algorithm, ...] = ()

# Distribution directory name -> its ``src`` sub-path. The audit scans source
# trees only; test trees (which hold the audit's own fixtures) are excluded.
_DISTRIBUTIONS: tuple[str, ...] = (
    "dexpace-sdk-core",
    "dexpace-sdk-http-stdlib",
    "dexpace-sdk-http-httpx",
    "dexpace-sdk-http-aiohttp",
    "dexpace-sdk-http-requests",
)


@dataclass(frozen=True)
class FunctionRecord:
    """One function definition located in a scanned tree.

    Attributes:
        module: Dotted module path relative to the scan root.
        name: The (possibly nested) function name.
        fingerprint: A hash of the normalised function body.
        statements: Number of body statements after stripping any docstring.
        lineno: 1-based line of the ``def``.
    """

    module: str
    name: str
    fingerprint: str
    statements: int
    lineno: int


@dataclass(frozen=True)
class Duplication:
    """A registered formula found restated in a second module."""

    algorithm: Algorithm
    canonical: FunctionRecord
    restated_in: FunctionRecord


def repo_root() -> Path:
    """Return the workspace root (the directory that holds ``packages/``)."""
    return Path(__file__).resolve().parent.parent


def source_roots(root: Path | None = None) -> list[Path]:
    """Return the ``src`` directory of every distribution that exists.

    Args:
        root: Workspace root to resolve against; defaults to the repository root.

    Returns:
        The existing per-distribution ``src`` roots, in registry order.
    """
    base = (root or repo_root()).resolve()
    roots = [base / "packages" / dist / "src" for dist in _DISTRIBUTIONS]
    return [path for path in roots if path.is_dir()]


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return ``body`` without a leading string-literal docstring statement."""
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def body_fingerprint(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, int]:
    """Fingerprint a function body, ignoring its name, signature and docstring.

    Two functions share a fingerprint exactly when their bodies are structurally
    identical, so a copy-pasted formula is caught even if the copy is renamed.

    Args:
        node: The (async) function definition to fingerprint.

    Returns:
        A ``(hex_digest, statement_count)`` pair for the normalised body.
    """
    statements = _strip_docstring(list(node.body))
    dumped = "\n".join(ast.dump(stmt, include_attributes=False) for stmt in statements)
    digest = hashlib.sha256(dumped.encode("utf-8")).hexdigest()
    return digest, len(statements)


def _dotted_module(src_root: Path, module_file: Path) -> str:
    """Return the dotted module path for a ``.py`` file under ``src_root``."""
    rel = module_file.relative_to(src_root).with_suffix("")
    return ".".join(rel.parts)


def collect_functions(roots: list[Path]) -> list[FunctionRecord]:
    """Collect every function definition under the given roots.

    Nested functions and methods are included so a formula restated inside a
    method body is still caught.

    Args:
        roots: Directories to walk for ``.py`` files.

    Returns:
        One ``FunctionRecord`` per function/method definition found.
    """
    records: list[FunctionRecord] = []
    for src_root in roots:
        for module_file in sorted(src_root.rglob("*.py")):
            module = _dotted_module(src_root, module_file)
            tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    digest, count = body_fingerprint(node)
                    records.append(FunctionRecord(module, node.name, digest, count, node.lineno))
    return records


def find_restated_algorithms(
    roots: list[Path], registry: tuple[Algorithm, ...]
) -> list[Duplication]:
    """Find registered formulas whose body is restated in another module.

    Args:
        roots: Source roots to scan.
        registry: Algorithms declared single-sourced.

    Returns:
        A ``Duplication`` for every module that restates a registered formula's
        canonical body. Empty when every formula is single-sourced.

    Raises:
        LookupError: If a registered algorithm's canonical function is not found
            in the scanned roots (a stale registry entry that must be fixed).
    """
    records = collect_functions(roots)
    duplications: list[Duplication] = []
    for algorithm in registry:
        canonical = _canonical_record(records, algorithm)
        for record in records:
            if record.module == canonical.module or record.fingerprint != canonical.fingerprint:
                continue
            duplications.append(Duplication(algorithm, canonical, record))
    return duplications


def _canonical_record(records: list[FunctionRecord], algorithm: Algorithm) -> FunctionRecord:
    """Return the canonical record for ``algorithm`` or raise if it is missing."""
    for record in records:
        if record.module == algorithm.module and record.name == algorithm.function:
            return record
    raise LookupError(
        f"single-sourced algorithm {algorithm.module}.{algorithm.function} "
        "was not found; update the registry in tools/check_uniqueness.py"
    )


def format_duplication(duplication: Duplication) -> str:
    """Render a duplication as a one-line, human-readable audit failure."""
    algo = duplication.algorithm
    restated = duplication.restated_in
    return (
        f"{algo.module}.{algo.function} ({algo.description}) is restated at "
        f"{restated.module}.{restated.name} (line {restated.lineno}); import the "
        "canonical definition instead of copying its body"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: audit the live tree and report any restated formulas.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when every single-sourced algorithm is unique, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.parse_args(argv)
    duplications = find_restated_algorithms(source_roots(), SINGLE_SOURCED)
    if not duplications:
        print(f"uniqueness audit: OK ({len(SINGLE_SOURCED)} single-sourced algorithm(s))")
        return 0
    print("uniqueness audit: FAILED", file=sys.stderr)
    for duplication in duplications:
        print(f"  - {format_duplication(duplication)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
