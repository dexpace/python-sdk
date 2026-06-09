# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Static extraction of the public API surface of every SDK distribution.

This module walks each distribution's ``src/`` tree with the standard-library
``ast`` parser — it never imports or executes project code, so it works without
the workspace being installed and cannot be tricked by import-time side effects.

The extracted surface has two halves per distribution:

- ``exports``: every ``__init__.py``'s ``__all__`` list, keyed by the dotted
  package path. This captures the deliberately narrow re-export surface that
  the project conventions require to stay accurate.
- ``definitions``: every public top-level class / function / Protocol defined
  anywhere in the tree, recorded with its signature (and, for classes, base
  classes and public method signatures). This catches a signature drifting or a
  ``with_*`` helper disappearing even when ``__all__`` is unchanged.

The resulting nested ``dict`` is plain JSON-serialisable data, sorted for a
stable diff. ``build_surface`` produces it; ``main`` writes it to the committed
baseline path. The companion pytest (``test_public_surface.py``) compares the
live surface against that baseline and fails on any unexpected change.

Run as a script to regenerate the baseline:

    python tools/surface_snapshot.py --write
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

# Distribution directory name -> the namespace sub-path under ``src`` that
# holds its modules. Every distribution shares the ``dexpace/sdk`` PEP-420
# namespace prefix; only the leaf differs.
_DISTRIBUTIONS: dict[str, str] = {
    "dexpace-sdk-core": "dexpace/sdk/core",
    "dexpace-sdk-http-stdlib": "dexpace/sdk/http/stdlib",
    "dexpace-sdk-http-httpx": "dexpace/sdk/http/httpx",
    "dexpace-sdk-http-aiohttp": "dexpace/sdk/http/aiohttp",
    "dexpace-sdk-http-requests": "dexpace/sdk/http/requests",
}

type Surface = dict[str, object]


def repo_root() -> Path:
    """Return the workspace root (the directory that holds ``packages/``).

    Returns:
        The absolute path to the repository root, resolved from this file's
        location (``tools/surface_snapshot.py`` sits directly under the root).
    """
    return Path(__file__).resolve().parent.parent


def baseline_path() -> Path:
    """Return the absolute path to the committed surface baseline JSON."""
    return repo_root() / "tools" / "surface_baseline.json"


def _is_public(name: str) -> bool:
    """Return whether ``name`` is part of the public surface (no leading ``_``).

    Dunder names (``__enter__`` and friends) are protocol hooks, not public
    API, so they are excluded alongside single-underscore privates.
    """
    return not name.startswith("_")


def _format_arg(arg: ast.arg) -> str:
    """Render one parameter as ``name`` or ``name: annotation``."""
    if arg.annotation is not None:
        return f"{arg.arg}: {ast.unparse(arg.annotation)}"
    return arg.arg


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render a function/method signature as a stable, source-independent string.

    Default *values* are deliberately omitted — only their presence matters for
    surface compatibility, and inlining a literal default makes the baseline
    churn on cosmetic edits. Parameter names, annotations, the positional/
    keyword-only split, and the return annotation are all preserved.

    Args:
        node: The (async) function definition node to render.

    Returns:
        A canonical one-line signature string, e.g.
        ``execute(self, request: Request) -> Response``.
    """
    args = node.args
    parts: list[str] = []
    parts.extend(_format_arg(a) for a in args.posonlyargs)
    if args.posonlyargs:
        parts.append("/")
    parts.extend(_format_arg(a) for a in args.args)
    if args.vararg is not None:
        parts.append(f"*{_format_arg(args.vararg)}")
    elif args.kwonlyargs:
        parts.append("*")
    parts.extend(_format_arg(a) for a in args.kwonlyargs)
    if args.kwarg is not None:
        parts.append(f"**{_format_arg(args.kwarg)}")
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}{node.name}({', '.join(parts)}){returns}"


def _class_surface(node: ast.ClassDef) -> dict[str, object]:
    """Extract the public surface of a class definition.

    Records its base classes (so a Protocol/ABC becoming a plain class, or a
    base disappearing, is caught) and the signature of each public method.

    Args:
        node: The class definition node.

    Returns:
        A mapping with ``bases`` (sorted base expressions) and ``methods``
        (method name -> signature string, public methods only).
    """
    bases = sorted(ast.unparse(b) for b in node.bases)
    methods: dict[str, str] = {}
    for item in node.body:
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(item.name):
            methods[item.name] = _format_signature(item)
    return {"bases": bases, "methods": dict(sorted(methods.items()))}


def _extract_all(module: ast.Module) -> list[str] | None:
    """Return the value of a module-level ``__all__`` list, or ``None``.

    Only a literal list/tuple of string constants is recognised — the project
    convention is to declare ``__all__`` as a plain literal, never built
    dynamically, so anything else is treated as "no static ``__all__``".

    Args:
        module: The parsed module node.

    Returns:
        The sorted list of exported names, or ``None`` if no static ``__all__``
        literal is present.
    """
    for stmt in module.body:
        if not isinstance(stmt, ast.Assign):
            continue
        targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
        if not any(t.id == "__all__" for t in targets):
            continue
        value = stmt.value
        if isinstance(value, ast.List | ast.Tuple):
            names = [
                el.value
                for el in value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
            return sorted(names)
    return None


def _module_definitions(module: ast.Module) -> dict[str, object]:
    """Extract public top-level class and function definitions from a module.

    Nested definitions are intentionally ignored — only the top-level surface
    of a module is API.

    Args:
        module: The parsed module node.

    Returns:
        A mapping of public symbol name -> its surface descriptor (a class
        descriptor mapping, or a function signature string).
    """
    defs: dict[str, object] = {}
    for stmt in module.body:
        if isinstance(stmt, ast.ClassDef) and _is_public(stmt.name):
            defs[stmt.name] = _class_surface(stmt)
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(stmt.name):
            defs[stmt.name] = _format_signature(stmt)
    return dict(sorted(defs.items()))


def _dotted_package(src_root: Path, init_file: Path) -> str:
    """Return the dotted package path for an ``__init__.py`` under ``src_root``.

    Args:
        src_root: The distribution's ``src`` directory.
        init_file: The ``__init__.py`` whose package path is wanted.

    Returns:
        The dotted package name, e.g. ``dexpace.sdk.core.http.request``.
    """
    rel = init_file.parent.relative_to(src_root)
    return ".".join(rel.parts)


def _dotted_module(src_root: Path, module_file: Path) -> str:
    """Return the dotted module path for a ``.py`` file under ``src_root``."""
    rel = module_file.relative_to(src_root).with_suffix("")
    return ".".join(rel.parts)


def _parse(path: Path) -> ast.Module:
    """Parse a Python source file into an AST module node."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _distribution_surface(src_root: Path) -> dict[str, object]:
    """Build the surface for a single distribution's ``src`` tree.

    Args:
        src_root: The distribution's ``src`` directory (the parent of the
            ``dexpace`` namespace directory).

    Returns:
        A mapping with ``exports`` (package -> ``__all__``) and ``definitions``
        (module -> public definitions), both sorted for a stable diff.
    """
    exports: dict[str, list[str]] = {}
    definitions: dict[str, object] = {}
    for module_file in sorted(src_root.rglob("*.py")):
        module = _parse(module_file)
        if module_file.name == "__init__.py":
            names = _extract_all(module)
            if names is not None:
                exports[_dotted_package(src_root, module_file)] = names
        module_defs = _module_definitions(module)
        if module_defs:
            definitions[_dotted_module(src_root, module_file)] = module_defs
    return {
        "exports": dict(sorted(exports.items())),
        "definitions": dict(sorted(definitions.items())),
    }


def build_surface(root: Path | None = None) -> Surface:
    """Build the public-API surface of every distribution by static analysis.

    Args:
        root: The workspace root to scan. Defaults to the repository root
            inferred from this file's location.

    Returns:
        A JSON-serialisable mapping of distribution name -> its surface. Only
        distributions whose ``src`` tree exists are included; the result is
        sorted by distribution name for a stable diff.

    Raises:
        FileNotFoundError: If the ``packages`` directory does not exist under
            ``root``.
    """
    base = (root or repo_root()).resolve()
    packages = base / "packages"
    if not packages.is_dir():
        raise FileNotFoundError(f"no packages directory under {base}")
    surface: dict[str, object] = {}
    for dist, namespace in sorted(_DISTRIBUTIONS.items()):
        src_root = packages / dist / "src"
        if not (src_root / namespace).is_dir():
            continue
        surface[dist] = _distribution_surface(src_root)
    return surface


def render(surface: Surface) -> str:
    """Render a surface mapping as canonical, newline-terminated JSON text."""
    return json.dumps(surface, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: print the live surface, or write it to the baseline.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        A process exit code (always ``0`` on success).
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the live surface to the committed baseline instead of printing it",
    )
    args = parser.parse_args(argv)
    surface = build_surface()
    text = render(surface)
    if args.write:
        baseline_path().write_text(text, encoding="utf-8")
        print(f"wrote baseline to {baseline_path()}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
