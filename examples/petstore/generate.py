# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Deterministic generator for the petstore codegen canary.

Reads the frozen OpenAPI document (``spec/petstore.openapi.json``) and renders
the checked-in SDK output under ``src/dexpace_petstore/_generated``:

- ``operations.py`` — the operation table (pure ``Operation`` data), and
- ``sync_client.py`` / ``async_client.py`` — two projection-only facades
  rendered from one method template with a sync/async mode switch.

The output is deterministic (operations sorted by id, stable ordering, no
timestamps), so re-running the generator reproduces the checked-in files
byte-for-byte — the property the regen-diff test asserts. Run it directly to
rewrite the checked-in files:

    python examples/petstore/generate.py

`render_all` is the pure library entry point (path -> content) the regen test
compares against the checked-in tree without touching the filesystem.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_HEADER = (
    "# Copyright (c) 2026 dexpace and Omar Aljarrah.\n"
    "# Licensed under the MIT License. See LICENSE.md in the repository root for details.\n"
)

#: HTTP methods recognised in a path item, in OpenAPI order.
_HTTP_METHODS = ("get", "put", "post", "delete", "patch")

#: Matches a single ``{name}`` path placeholder.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

_SPEC_PATH = Path(__file__).resolve().parent / "spec" / "petstore.openapi.json"
_OUT_DIR = Path(__file__).resolve().parent / "src" / "dexpace_petstore" / "_generated"


@dataclass(frozen=True, slots=True)
class OpSpec:
    """One operation drawn from the frozen document, normalised for rendering.

    Attributes:
        operation_id: The stable method / constant name.
        method: The HTTP method, upper-cased (``"GET"``, ``"PATCH"``).
        path: The path template, ``{name}`` placeholders intact.
        kind: The facade shape — ``"unary"``, ``"paginate"``, or ``"events"``.
        path_params: The ``{name}`` placeholders, in path order.
        returns: The response model name for a unary op, else ``None``.
        body_param: The request-body parameter name for a unary op, else
            ``None``.
        body_model: The request-body model name, else ``None``.
        item_model: The page item model for a paginate op, else ``None``.
        strategy: The pagination-strategy binder symbol, else ``None``.
        event_model: The event model for an events op, else ``None``.
        mapper: The SSE-mapper binder symbol, else ``None``.
        auth: The operation-level auth alternatives, each a ``(scheme, scopes)``
            pair; empty when the op declares no requirement.
    """

    operation_id: str
    method: str
    path: str
    kind: str
    path_params: tuple[str, ...]
    returns: str | None
    body_param: str | None
    body_model: str | None
    item_model: str | None
    strategy: str | None
    event_model: str | None
    mapper: str | None
    auth: tuple[tuple[str, tuple[str, ...]], ...]


def load_spec(spec_path: Path = _SPEC_PATH) -> dict[str, object]:
    """Parse and return the frozen OpenAPI document.

    Args:
        spec_path: Path to the JSON document. Defaults to the checked-in spec.

    Returns:
        The parsed document as a plain dictionary.
    """
    parsed: dict[str, object] = json.loads(spec_path.read_text(encoding="utf-8"))
    return parsed


def collect_operations(spec: dict[str, object]) -> list[OpSpec]:
    """Extract every operation from the document, sorted by operation id.

    Args:
        spec: The parsed OpenAPI document.

    Returns:
        The operations in a stable, id-sorted order so rendering is
        deterministic.
    """
    paths = spec.get("paths", {})
    assert isinstance(paths, dict)
    ops: list[OpSpec] = []
    for path, item in paths.items():
        assert isinstance(item, dict)
        for method in _HTTP_METHODS:
            entry = item.get(method)
            if entry is not None:
                assert isinstance(entry, dict)
                ops.append(_op_from_entry(path, method, entry))
    return sorted(ops, key=lambda op: op.operation_id)


def _op_from_entry(path: str, method: str, entry: dict[str, object]) -> OpSpec:
    """Normalise one path-item operation into an `OpSpec`."""
    ext = entry.get("x-dexpace", {})
    assert isinstance(ext, dict)
    body = ext.get("body")
    body_map = body if isinstance(body, dict) else {}
    return OpSpec(
        operation_id=str(entry["operationId"]),
        method=method.upper(),
        path=path,
        kind=str(ext["kind"]),
        path_params=tuple(_PLACEHOLDER.findall(path)),
        returns=_opt_str(ext.get("returns")),
        body_param=_opt_str(body_map.get("param")),
        body_model=_opt_str(body_map.get("model")),
        item_model=_opt_str(ext.get("item_model")),
        strategy=_opt_str(ext.get("strategy")),
        event_model=_opt_str(ext.get("event_model")),
        mapper=_opt_str(ext.get("mapper")),
        auth=_auth_of(ext.get("auth")),
    )


def _opt_str(value: object) -> str | None:
    """Return ``value`` as a string, or ``None`` when it is absent."""
    return None if value is None else str(value)


def _auth_of(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Normalise the ``auth`` extension into ``(scheme, scopes)`` pairs."""
    if not isinstance(value, list):
        return ()
    out: list[tuple[str, tuple[str, ...]]] = []
    for alt in value:
        assert isinstance(alt, dict)
        scopes = alt.get("scopes", [])
        assert isinstance(scopes, list)
        out.append((str(alt["scheme"]), tuple(str(s) for s in scopes)))
    return tuple(out)


_OPERATIONS_DOC = (
    '"""Operation table for the petstore canary — generated; do not edit.\n'
    "\n"
    "Rendered from the frozen OpenAPI document by ``examples/petstore/generate.py``.\n"
    "Pure data: one ``Operation`` declaration per API operation, imported verbatim\n"
    'by both the sync and async facades.\n"""\n'
)

_SYNC_FACADE_DOC = (
    '"""Synchronous petstore facade — generated; do not edit.\n'
    "\n"
    "Rendered from the frozen OpenAPI document by ``examples/petstore/generate.py``.\n"
    "A projection-only wrapper: every method binds its arguments into an\n"
    "``OperationInput`` and delegates to the shared ``ServiceCore`` executor,\n"
    'carrying no logic of its own.\n"""\n'
)

_ASYNC_FACADE_DOC = (
    '"""Asynchronous petstore facade — generated; do not edit.\n'
    "\n"
    "The async twin of the sync facade. Rendered from the same method template by\n"
    "``examples/petstore/generate.py`` with the async mode switch, so it normalises\n"
    "to the sync facade's AST under the parity gate. A projection-only wrapper over\n"
    'the shared ``AsyncServiceCore`` executor.\n"""\n'
)

_SYNC_CLASS_DOC = '"""Synchronous petstore client — a projection over ``ServiceCore``."""'
_ASYNC_CLASS_DOC = '"""Asynchronous petstore client — a projection over ``AsyncServiceCore``."""'


def render_operations(ops: list[OpSpec]) -> str:
    """Render the operation-table module from the collected operations."""
    consts = [op.operation_id.upper() for op in ops]
    all_line = "__all__ = [" + ", ".join(f'"{c}"' for c in consts) + "]"
    imports = "from dexpace.sdk.core.codegen import "
    imports += (
        "AuthDescriptor, AuthRequirement, Operation" if any(op.auth for op in ops) else "Operation"
    )
    body: list[str] = [_HEADER, "\n", _OPERATIONS_DOC, "\nfrom __future__ import annotations\n\n"]
    body.append(imports + "\nfrom dexpace.sdk.core.http.request import Method\n\n")
    body.append(all_line + "\n")
    auth_lines = [_auth_descriptor(op) for op in ops if op.auth]
    if auth_lines:
        body.append("\n" + "\n".join(auth_lines) + "\n")
    body.append("\n" + "\n".join(_operation_line(op) for op in ops) + "\n")
    return "".join(body)


def _auth_descriptor(op: OpSpec) -> str:
    """Render the module-private auth-descriptor binding for one operation."""
    reqs = ", ".join(_render_requirement(scheme, scopes) for scheme, scopes in op.auth)
    return f"{_auth_name(op)} = AuthDescriptor.of({reqs})"


def _auth_name(op: OpSpec) -> str:
    """Return the module-private auth-descriptor variable name for an operation."""
    return f"_{op.operation_id.upper()}_AUTH"


def _render_requirement(scheme: str, scopes: tuple[str, ...]) -> str:
    """Render one ``AuthRequirement`` literal, scopes included when present."""
    if not scopes:
        return f'AuthRequirement("{scheme}")'
    items = ", ".join(f'"{s}"' for s in scopes)
    tail = "," if len(scopes) == 1 else ""
    return f'AuthRequirement("{scheme}", ({items}{tail}))'


def _operation_line(op: OpSpec) -> str:
    """Render one ``Operation`` declaration line."""
    parts = [
        f'name="{op.operation_id}"',
        f"method=Method.{op.method}",
        f'path="{op.path}"',
    ]
    if op.auth:
        parts.append(f"auth={_auth_name(op)}")
    return f"{op.operation_id.upper()} = Operation({', '.join(parts)})"


def render_facade(ops: list[OpSpec], *, mode: str) -> str:
    """Render one facade module (``mode`` is ``"sync"`` or ``"async"``)."""
    async_ = mode == "async"
    cls = "AsyncPetStoreClient" if async_ else "PetStoreClient"
    core_t = "AsyncServiceCore" if async_ else "ServiceCore"
    doc = _ASYNC_FACADE_DOC if async_ else _SYNC_FACADE_DOC
    class_doc = _ASYNC_CLASS_DOC if async_ else _SYNC_CLASS_DOC
    preamble = [_HEADER, "\n", doc, "\n", _facade_imports(ops, mode), "\n"]
    preamble.append(f'__all__ = ["{cls}"]\n\n\n')
    head = [
        f"class {cls}:",
        f"    {class_doc}",
        "",
        '    __slots__ = ("_core",)',
        "",
        f"    def __init__(self, core: {core_t}) -> None:",
        "        self._core = core",
    ]
    blocks = ["\n".join(head)]
    blocks += [_render_method(op, mode) for op in ops]
    blocks.append(_render_lifecycle(mode))
    return "".join(preamble) + "\n\n".join(blocks) + "\n"


def _facade_imports(ops: list[OpSpec], mode: str) -> str:
    """Render the full import block (runtime + ``TYPE_CHECKING``) for a facade."""
    async_ = mode == "async"
    support = sorted(
        ({"json_body"} if any(op.body_param for op in ops) else set())
        | {op.strategy for op in ops if op.strategy}
        | {op.mapper for op in ops if op.mapper}
    )
    runtime_models = sorted({op.returns for op in ops if op.returns})
    tc_models = sorted(
        (
            {op.body_model for op in ops if op.body_model}
            | {op.item_model for op in ops if op.item_model}
            | {op.event_model for op in ops if op.event_model}
        )
        - set(runtime_models)
    )
    runtime = [
        "from __future__ import annotations\n\n",
        "from typing import TYPE_CHECKING, Self\n\n",
        "from dexpace.sdk.core.codegen import OperationInput\n\n",
        f"from .._support import {', '.join(support)}\n",
        f"from ..models import {', '.join(runtime_models)}\n",
        "from . import operations\n",
    ]
    return "".join(runtime) + _facade_tc_imports(ops, async_, tc_models)


def _facade_tc_imports(ops: list[OpSpec], async_: bool, tc_models: list[str]) -> str:
    """Render the ``if TYPE_CHECKING:`` import block for a facade."""
    core_t = "AsyncServiceCore" if async_ else "ServiceCore"
    third = [f"    from dexpace.sdk.core.codegen import {core_t}\n"]
    if any(op.kind == "events" for op in ops):
        sse = "AsyncTypedSseConnection" if async_ else "TypedSseConnection"
        third.append(f"    from dexpace.sdk.core.http.sse.typed import {sse}\n")
    if any(op.kind == "paginate" for op in ops):
        pag = "AsyncPaginator" if async_ else "Paginator"
        third.append(f"    from dexpace.sdk.core.pagination import {pag}\n")
    block = [
        "\nif TYPE_CHECKING:\n",
        "    from types import TracebackType\n\n",
        "".join(third),
    ]
    if tc_models:
        block.append(f"\n    from ..models import {', '.join(tc_models)}\n")
    return "".join(block)


def _render_method(op: OpSpec, mode: str) -> str:
    """Dispatch to the per-kind method renderer."""
    if op.kind == "paginate":
        return _paginate_method(op, mode)
    if op.kind == "events":
        return _events_method(op, mode)
    return _unary_method(op, mode)


def _unary_method(op: OpSpec, mode: str) -> str:
    """Render a unary (single request/response) method."""
    async_kw = "async " if mode == "async" else ""
    await_kw = "await " if mode == "async" else ""
    sig = f"    {async_kw}def {op.operation_id}({_unary_params(op)}) -> {op.returns}:"
    args = [
        f"            operations.{op.operation_id.upper()},",
        f"            {_operation_input(op)},",
        f"            response_type={op.returns},",
    ]
    lines = [sig, f"        return {await_kw}self._core.execute(", *args, "        )"]
    return "\n".join(lines)


def _unary_params(op: OpSpec) -> str:
    """Render a unary method's parameter list (path params, then any body)."""
    params = ["self", *(f"{name}: str" for name in op.path_params)]
    if op.body_param is not None:
        params.append(f"{op.body_param}: {op.body_model}")
    return ", ".join(params)


def _operation_input(op: OpSpec) -> str:
    """Render the ``OperationInput(...)`` construction for a unary method."""
    args: list[str] = []
    if op.path_params:
        pairs = ", ".join(f'"{name}": {name}' for name in op.path_params)
        args.append(f"path_params={{{pairs}}}")
    if op.body_param is not None:
        args.append(f"body=json_body({op.body_param})")
    return f"OperationInput({', '.join(args)})"


def _paginate_method(op: OpSpec, mode: str) -> str:
    """Render a paginate method returning the certified paginator."""
    pag = "AsyncPaginator" if mode == "async" else "Paginator"
    sig = f"    def {op.operation_id}(self, *, max_pages: int | None = None) -> {pag}[{op.item_model}]:"
    lines = [
        sig,
        "        return self._core.paginate(",
        f"            operations.{op.operation_id.upper()},",
        "            OperationInput(),",
        f"            {op.strategy},",
        "            max_pages=max_pages,",
        "        )",
    ]
    return "\n".join(lines)


def _events_method(op: OpSpec, mode: str) -> str:
    """Render an events method returning the certified typed SSE connection."""
    sse = "AsyncTypedSseConnection" if mode == "async" else "TypedSseConnection"
    sig = f"    def {op.operation_id}(self) -> {sse}[{op.event_model}]:"
    lines = [
        sig,
        "        return self._core.events(",
        f"            operations.{op.operation_id.upper()},",
        "            OperationInput(),",
        f"            {op.mapper},",
        "        )",
    ]
    return "\n".join(lines)


def _render_lifecycle(mode: str) -> str:
    """Render the context-manager / close methods for a facade."""
    if mode == "async":
        return (
            "    async def aclose(self) -> None:\n"
            "        await self._core.aclose()\n\n"
            "    async def __aenter__(self) -> Self:\n"
            "        return self\n\n"
            "    async def __aexit__(\n"
            "        self,\n"
            "        exc_type: type[BaseException] | None,\n"
            "        exc: BaseException | None,\n"
            "        tb: TracebackType | None,\n"
            "    ) -> None:\n"
            "        await self.aclose()"
        )
    return (
        "    def close(self) -> None:\n"
        "        self._core.close()\n\n"
        "    def __enter__(self) -> Self:\n"
        "        return self\n\n"
        "    def __exit__(\n"
        "        self,\n"
        "        exc_type: type[BaseException] | None,\n"
        "        exc: BaseException | None,\n"
        "        tb: TracebackType | None,\n"
        "    ) -> None:\n"
        "        self.close()"
    )


def render_all(spec_path: Path = _SPEC_PATH) -> dict[str, str]:
    """Render every generated file as ``relative path -> content``.

    Args:
        spec_path: Path to the frozen document.

    Returns:
        A mapping from each output file's name to its full rendered text. Pure:
        it touches no filesystem beyond reading the spec.
    """
    ops = collect_operations(load_spec(spec_path))
    return {
        "operations.py": render_operations(ops),
        "sync_client.py": render_facade(ops, mode="sync"),
        "async_client.py": render_facade(ops, mode="async"),
    }


def write_all(spec_path: Path = _SPEC_PATH, out_dir: Path = _OUT_DIR) -> None:
    """Render and write every generated file into ``out_dir``.

    Args:
        spec_path: Path to the frozen document.
        out_dir: Directory the ``_generated`` modules are written into.
    """
    for name, content in render_all(spec_path).items():
        (out_dir / name).write_text(content, encoding="utf-8")


def main() -> int:
    """Rewrite the checked-in generated files. Returns a process exit code."""
    write_all()
    print(f"generated {len(render_all())} files into {_OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
