# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Blocking sync/async parity gate over the two generated facades.

The generated facades are rendered from one method template with a sync/async
mode switch, so after the parity tool strips ``async``/``await`` and applies the
declared token map they must normalise to identical ASTs. This runs the gate in
BLOCKING mode over the checked-in facades (any residual drift fails) and, with a
seeded one-line drift in an in-memory scratch copy, proves the gate has teeth —
the real facades on disk are never modified.

The token map extends the tool's default with the facade-specific renames
(``AsyncServiceCore`` -> ``ServiceCore``, ``AsyncPaginator`` -> ``Paginator``,
``AsyncTypedSseConnection`` -> ``TypedSseConnection``, ``AsyncPetStoreClient`` ->
``PetStoreClient``). The tool is loaded by file path, as it is not an installed
module.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GENERATED = _REPO_ROOT / "examples" / "petstore" / "src" / "dexpace_petstore" / "_generated"
_SYNC = _GENERATED / "sync_client.py"
_ASYNC = _GENERATED / "async_client.py"

#: Facade-specific renames layered onto the tool's default token map.
_FACADE_TOKENS = {
    "AsyncServiceCore": "ServiceCore",
    "AsyncPaginator": "Paginator",
    "AsyncTypedSseConnection": "TypedSseConnection",
    "AsyncPetStoreClient": "PetStoreClient",
}


def _load_parity() -> Any:
    """Load ``tools/parity_check.py`` by file path.

    Returns:
        The imported parity-check module. Typed ``Any`` because it is loaded
        dynamically, not resolved as a static import.

    Raises:
        ImportError: If the parity tool cannot be loaded.
    """
    path = _REPO_ROOT / "tools" / "parity_check.py"
    spec = importlib.util.spec_from_file_location("parity_check", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the parity tool from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _token_map(parity: Any) -> dict[str, str]:
    token_map: dict[str, str] = dict(parity.DEFAULT_TOKEN_MAP)
    token_map.update(_FACADE_TOKENS)
    return token_map


def test_generated_facades_normalise_to_identical_asts() -> None:
    parity = _load_parity()
    result = parity.check_parity(
        _SYNC, _ASYNC, token_map=_token_map(parity), mode=parity.Mode.BLOCKING
    )
    assert not result.failed, result.diff
    assert result.in_parity


def test_parity_gate_fails_on_seeded_drift() -> None:
    parity = _load_parity()
    sync_src = _SYNC.read_text(encoding="utf-8")
    async_src = _ASYNC.read_text(encoding="utf-8")
    # Seed a single semantic drift into a scratch copy only — never on disk.
    drifted = async_src.replace("max_pages=max_pages,", "max_pages=None,", 1)
    assert drifted != async_src
    result = parity.check_parity_sources(
        sync_src, drifted, token_map=_token_map(parity), mode=parity.Mode.BLOCKING
    )
    assert result.failed
