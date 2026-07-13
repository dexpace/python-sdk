# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Sync/async parity gate — AST-normalized diff of a sync/async source pair.

An async twin of a sync module is meant to be a mechanical transliteration:
the same control flow and calls, with ``async``/``await`` added and a small,
fixed set of identifiers swapped (``AsyncResponse`` for ``Response``,
``AsyncPolicy`` for ``Policy``, and so on). This tool verifies that guarantee.
It normalizes both files' abstract syntax trees through a *closed, declared*
token map — stripping ``await``, mapping ``async def`` / ``async for`` /
``async with`` to their sync node types, and renaming a declared set of
identifiers — then compares the resulting trees structurally. Any residual
difference after normalization is semantic drift.

The comparison is on AST structure, never source text: ``ast.unparse`` discards
comments and reformats, so a textual diff would report cosmetic noise as drift
and miss real drift hidden behind formatting. Normalization is a fixed token
table, never fuzzy matching — an identifier the table does not name is compared
as-is, so an accidental rename is caught rather than smoothed over.

Two modes:

- **Blocking** (`Mode.BLOCKING`): intended for *generated* sync/async pairs,
  where AST identity after normalization is the correctness contract. A
  non-empty normalized diff is a failure (non-zero exit).
- **Report-only** (`Mode.REPORT_ONLY`): intended for the existing hand-written
  policy twins, whose correctness is guaranteed by a different mechanism
  (import-linter contracts), not textual identity. It emits the diff artifact
  but never fails, so those legitimately-divergent twins (delegating wrappers,
  extra async-only helpers) do not break the build.

Library API: `check_parity` / `check_parity_sources` / `normalize_source`.
CLI:

    python tools/parity_check.py SYNC.py ASYNC.py --mode blocking
    python tools/parity_check.py SYNC.py ASYNC.py --mode report-only --report-out diff.txt
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Default identifier/token map from the async tree to the sync tree. Keys that
#: contain a ``.`` are dotted attribute chains (``asyncio.sleep`` matched as a
#: whole); keys without a ``.`` are single identifiers matched wherever they
#: appear (names, attributes, class/function/parameter names, import segments).
DEFAULT_TOKEN_MAP: Mapping[str, str] = {
    # Pipeline base types and their modules.
    "AsyncPolicy": "Policy",
    "async_policy": "policy",
    # Response model and its module.
    "AsyncResponse": "Response",
    "async_response": "response",
    # Clock seam.
    "AsyncClock": "Clock",
    "ASYNC_SYSTEM_CLOCK": "SYSTEM_CLOCK",
    # Shipped policy class twins.
    "AsyncSetDatePolicy": "SetDatePolicy",
    "AsyncClientIdentityPolicy": "ClientIdentityPolicy",
    "AsyncRedirectPolicy": "RedirectPolicy",
    "AsyncRetryPolicy": "RetryPolicy",
    "AsyncIdempotencyPolicy": "IdempotencyPolicy",
    "AsyncTracingPolicy": "TracingPolicy",
    # Generic async protocol method / builtin names.
    "aiter": "iter",
    "aiter_bytes": "iter_bytes",
    "aclose": "close",
    "anext": "next",
    "__aenter__": "__enter__",
    "__aexit__": "__exit__",
    # Awaitable sleep to its synchronous wait equivalent.
    "asyncio.sleep": "time.sleep",
}


class Mode(Enum):
    """How a parity check reacts to a non-empty normalized diff.

    Attributes:
        BLOCKING: Drift is a failure; the CLI exits non-zero.
        REPORT_ONLY: Drift is recorded in the artifact but never fails.
    """

    BLOCKING = "blocking"
    REPORT_ONLY = "report-only"


@dataclass(frozen=True, slots=True)
class ParityResult:
    """Outcome of comparing one normalized sync/async source pair.

    Attributes:
        sync_label: Human-readable label (path) for the sync source.
        async_label: Human-readable label (path) for the async source.
        in_parity: ``True`` when the normalized ASTs are identical.
        diff: Unified diff of the two normalized AST dumps; empty in parity.
        mode: The mode the check ran under.
    """

    sync_label: str
    async_label: str
    in_parity: bool
    diff: str
    mode: Mode

    @property
    def failed(self) -> bool:
        """Return whether this result should fail the build.

        Returns:
            ``True`` only when drift was found *and* the mode is blocking.
        """
        return not self.in_parity and self.mode is Mode.BLOCKING


def _split_token_map(token_map: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Partition a token map into single-identifier and dotted-chain entries.

    Args:
        token_map: The declared async-to-sync token table.

    Returns:
        A ``(simple, dotted)`` pair of mappings; ``dotted`` holds every key that
        contains a ``.`` (matched as a whole attribute chain), ``simple`` the
        rest (matched per identifier).
    """
    simple: dict[str, str] = {}
    dotted: dict[str, str] = {}
    for key, value in token_map.items():
        (dotted if "." in key else simple)[key] = value
    return simple, dotted


def _dotted_path(node: ast.expr) -> str | None:
    """Return the dotted name of a pure ``Name``/``Attribute`` chain, else None.

    Args:
        node: An expression node.

    Returns:
        The reconstructed dotted string (``a.b.c``) when ``node`` is a chain of
        attribute accesses bottoming out in a bare name, otherwise ``None``
        (e.g. when the base is a call or subscript).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_path(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


class _StructuralNormalizer(ast.NodeTransformer):
    """Collapse async syntax onto its synchronous node shapes.

    Rewrites ``async def`` to ``def``, ``async for``/``async with`` to
    ``for``/``with``, drops each ``await`` wrapper, and clears the ``is_async``
    flag on comprehension clauses. Identifier renaming is a separate pass.
    """

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        sync = ast.FunctionDef(
            name=node.name,
            args=node.args,
            body=node.body,
            decorator_list=node.decorator_list,
            returns=node.returns,
            type_comment=node.type_comment,
            type_params=node.type_params,
        )
        return self.generic_visit(sync)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.AST:
        sync = ast.For(
            target=node.target,
            iter=node.iter,
            body=node.body,
            orelse=node.orelse,
            type_comment=node.type_comment,
        )
        return self.generic_visit(sync)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.AST:
        sync = ast.With(items=node.items, body=node.body, type_comment=node.type_comment)
        return self.generic_visit(sync)

    def visit_Await(self, node: ast.Await) -> ast.AST:
        # Drop the ``await`` wrapper, keeping (and recursing into) its operand.
        replacement = self.visit(node.value)
        assert isinstance(replacement, ast.AST)
        return replacement

    def visit_comprehension(self, node: ast.comprehension) -> ast.AST:
        node.is_async = 0
        return self.generic_visit(node)


class _DocstringStripper(ast.NodeTransformer):
    """Drop the leading string-literal docstring from every module/class/def.

    Docstrings are prose, not behaviour; an async twin legitimately documents
    itself as an "async twin of ...". Removing them keeps the comparison on
    semantics. General string literals (log messages, header values) are left
    intact so a changed message still registers as drift.
    """

    @staticmethod
    def _strip(body: list[ast.stmt]) -> list[ast.stmt]:
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return body[1:]
        return body

    def visit_Module(self, node: ast.Module) -> ast.AST:
        node.body = self._strip(node.body)
        return self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node.body = self._strip(node.body)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.body = self._strip(node.body)
        return self.generic_visit(node)


class _DottedRenamer(ast.NodeTransformer):
    """Rewrite whole dotted attribute chains named in the dotted token map."""

    def __init__(self, dotted: Mapping[str, str]) -> None:
        self._dotted = dotted

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        dotted = _dotted_path(node)
        if dotted is not None and dotted in self._dotted:
            replacement = ast.parse(self._dotted[dotted], mode="eval").body
            return replacement
        return self.generic_visit(node)


class _IdentifierRenamer(ast.NodeTransformer):
    """Rename single identifiers wherever they appear, per the simple map.

    Covers names, attribute labels, class/function/parameter names, keyword
    argument labels, and import segments. Import module paths and dotted import
    aliases are renamed segment by segment, so ``async_response`` inside
    ``...http.response.async_response`` maps without a dedicated dotted entry.
    A string constant that *exactly* equals a token key is also renamed (whole
    string, never a substring), so ``__all__`` entries and other name-as-string
    references line up; a string that merely contains a token is left untouched
    so real message drift still registers.
    """

    def __init__(self, simple: Mapping[str, str]) -> None:
        self._simple = simple

    def _rename(self, name: str) -> str:
        return self._simple.get(name, name)

    def _rename_dotted(self, dotted: str) -> str:
        return ".".join(self._rename(part) for part in dotted.split("."))

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = self._rename(node.id)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str) and node.value in self._simple:
            node.value = self._simple[node.value]
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        node.attr = self._rename(node.attr)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.name = self._rename(node.name)
        return self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node.name = self._rename(node.name)
        return self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = self._rename(node.arg)
        return self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> ast.AST:
        if node.arg is not None:
            node.arg = self._rename(node.arg)
        return self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> ast.AST:
        node.name = self._rename_dotted(node.name)
        if node.asname is not None:
            node.asname = self._rename(node.asname)
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        if node.module is not None:
            node.module = self._rename_dotted(node.module)
        return self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> ast.AST:
        node.names = [self._rename(name) for name in node.names]
        return node

    def visit_Nonlocal(self, node: ast.Nonlocal) -> ast.AST:
        node.names = [self._rename(name) for name in node.names]
        return node


def normalize_source(
    source: str,
    token_map: Mapping[str, str] | None = None,
    *,
    keep_docstrings: bool = False,
) -> ast.Module:
    """Parse ``source`` and normalize its AST through the token map.

    The pipeline is fixed and declared: collapse async syntax, optionally drop
    docstrings, rewrite dotted chains, then rename single identifiers.

    Args:
        source: Python source text.
        token_map: Async-to-sync token table. Defaults to `DEFAULT_TOKEN_MAP`.
        keep_docstrings: When ``True``, leave docstrings in place instead of
            stripping them before the comparison.

    Returns:
        The normalized module AST, ready to dump or compare.
    """
    tokens = DEFAULT_TOKEN_MAP if token_map is None else token_map
    simple, dotted = _split_token_map(tokens)
    tree: ast.AST = ast.parse(source)
    tree = _StructuralNormalizer().visit(tree)
    if not keep_docstrings:
        tree = _DocstringStripper().visit(tree)
    tree = _DottedRenamer(dotted).visit(tree)
    tree = _IdentifierRenamer(simple).visit(tree)
    ast.fix_missing_locations(tree)
    assert isinstance(tree, ast.Module)
    return tree


def normalized_dump(
    source: str,
    token_map: Mapping[str, str] | None = None,
    *,
    keep_docstrings: bool = False,
) -> str:
    """Return the canonical, position-free AST dump of normalized ``source``.

    Args:
        source: Python source text.
        token_map: Async-to-sync token table. Defaults to `DEFAULT_TOKEN_MAP`.
        keep_docstrings: Forwarded to `normalize_source`.

    Returns:
        An indented ``ast.dump`` string; equal dumps mean AST parity.
    """
    tree = normalize_source(source, token_map, keep_docstrings=keep_docstrings)
    return ast.dump(tree, indent=2)


def check_parity_sources(
    sync_source: str,
    async_source: str,
    *,
    sync_label: str = "sync",
    async_label: str = "async",
    token_map: Mapping[str, str] | None = None,
    mode: Mode = Mode.BLOCKING,
    keep_docstrings: bool = False,
) -> ParityResult:
    """Compare two source strings after normalization.

    Both sources are normalized to the sync shape and their AST dumps compared.
    When they differ, a unified diff of the dumps is produced (structural, not
    textual — it diffs AST node trees, not source lines).

    Args:
        sync_source: The sync (reference) source text.
        async_source: The async twin source text.
        sync_label: Label for the sync side in diagnostics and the diff header.
        async_label: Label for the async side.
        token_map: Async-to-sync token table. Defaults to `DEFAULT_TOKEN_MAP`.
        mode: Blocking or report-only.
        keep_docstrings: Forwarded to `normalize_source`.

    Returns:
        A `ParityResult` describing whether the pair is in parity.
    """
    sync_dump = normalized_dump(sync_source, token_map, keep_docstrings=keep_docstrings)
    async_dump = normalized_dump(async_source, token_map, keep_docstrings=keep_docstrings)
    in_parity = sync_dump == async_dump
    diff = "" if in_parity else _render_diff(sync_dump, async_dump, sync_label, async_label)
    return ParityResult(sync_label, async_label, in_parity, diff, mode)


def _render_diff(sync_dump: str, async_dump: str, sync_label: str, async_label: str) -> str:
    """Render a unified diff of two normalized AST dumps."""
    lines = difflib.unified_diff(
        sync_dump.splitlines(keepends=True),
        async_dump.splitlines(keepends=True),
        fromfile=f"{sync_label} (normalized AST)",
        tofile=f"{async_label} (normalized AST)",
    )
    return "".join(lines)


def check_parity(
    sync_path: str | Path,
    async_path: str | Path,
    *,
    token_map: Mapping[str, str] | None = None,
    mode: Mode = Mode.BLOCKING,
    keep_docstrings: bool = False,
) -> ParityResult:
    """Compare a sync/async file pair after normalization.

    Args:
        sync_path: Path to the sync (reference) module.
        async_path: Path to the async twin module.
        token_map: Async-to-sync token table. Defaults to `DEFAULT_TOKEN_MAP`.
        mode: Blocking or report-only.
        keep_docstrings: Forwarded to `normalize_source`.

    Returns:
        A `ParityResult` describing whether the pair is in parity.
    """
    sync_p, async_p = Path(sync_path), Path(async_path)
    return check_parity_sources(
        sync_p.read_text(encoding="utf-8"),
        async_p.read_text(encoding="utf-8"),
        sync_label=str(sync_p),
        async_label=str(async_p),
        token_map=token_map,
        mode=mode,
        keep_docstrings=keep_docstrings,
    )


def _load_token_map(path: Path | None) -> Mapping[str, str]:
    """Load a token map from a JSON file, or return the default.

    Args:
        path: Path to a JSON object of string-to-string entries, or ``None``.

    Returns:
        The parsed token map, or `DEFAULT_TOKEN_MAP` when ``path`` is ``None``.

    Raises:
        ValueError: If the JSON is not an object of string-to-string entries.
    """
    if path is None:
        return DEFAULT_TOKEN_MAP
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ValueError(f"token map at {path} must be a JSON object of string -> string")
    return raw


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        description="AST-normalized sync/async parity check.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("sync", type=Path, help="path to the sync (reference) module")
    parser.add_argument("async_", metavar="async", type=Path, help="path to the async twin module")
    parser.add_argument(
        "--mode",
        type=Mode,
        default=Mode.BLOCKING,
        choices=list(Mode),
        help="blocking (drift fails) or report-only (drift recorded, never fails)",
    )
    parser.add_argument(
        "--token-map", type=Path, default=None, help="JSON file overriding the default token map"
    )
    parser.add_argument(
        "--keep-docstrings",
        action="store_true",
        help="compare docstrings instead of stripping them",
    )
    parser.add_argument(
        "--report-out", type=Path, default=None, help="write the diff artifact to this path"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for a single sync/async pair.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when in parity or in report-only mode; ``1`` on drift in blocking
        mode.
    """
    args = _parse_args(argv)
    result = check_parity(
        args.sync,
        args.async_,
        token_map=_load_token_map(args.token_map),
        mode=args.mode,
        keep_docstrings=args.keep_docstrings,
    )
    if args.report_out is not None:
        artifact = result.diff or "# in parity: normalized ASTs are identical\n"
        args.report_out.write_text(artifact, encoding="utf-8")
    if result.in_parity:
        print(f"parity OK: {args.sync} == {args.async_} (normalized)")
        return 0
    sys.stdout.write(result.diff)
    verdict = "FAIL" if result.failed else "drift (report-only)"
    print(f"parity {verdict}: {args.sync} vs {args.async_}", file=sys.stderr)
    return 1 if result.failed else 0


__all__ = [
    "DEFAULT_TOKEN_MAP",
    "Mode",
    "ParityResult",
    "check_parity",
    "check_parity_sources",
    "main",
    "normalize_source",
    "normalized_dump",
]


if __name__ == "__main__":
    raise SystemExit(main())
