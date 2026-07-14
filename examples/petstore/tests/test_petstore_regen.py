# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Regen-diff guard for the petstore canary.

Re-runs the deterministic generator over the frozen OpenAPI document and asserts
its output matches the checked-in ``_generated`` tree exactly. A hand-edit to any
generated file — or a generator change not reflected in the checked-in output —
fails here, so the canary can only be regenerated, never hand-patched. The
generator is loaded by file path (it is a script, not an installed module), the
same way the parity and public-surface tests load their sibling tools.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PETSTORE = _REPO_ROOT / "examples" / "petstore"
_GENERATED = _PETSTORE / "src" / "dexpace_petstore" / "_generated"


def _load_generator() -> Any:
    """Load ``examples/petstore/generate.py`` by file path.

    Returns:
        The imported generator module. Typed ``Any`` because it is loaded
        dynamically, not resolved as a static import.

    Raises:
        ImportError: If the generator module cannot be loaded.
    """
    path = _PETSTORE / "generate.py"
    spec = importlib.util.spec_from_file_location("petstore_generate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the petstore generator from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_regenerating_reproduces_checked_in_output() -> None:
    rendered: dict[str, str] = _load_generator().render_all()
    for name, content in rendered.items():
        checked_in = (_GENERATED / name).read_text(encoding="utf-8")
        assert content == checked_in, (
            f"{name} is out of sync with the generator; re-run "
            "`python examples/petstore/generate.py` (never hand-edit generated files)"
        )


def test_generator_accounts_for_every_generated_module() -> None:
    rendered = set(_load_generator().render_all())
    on_disk = {path.name for path in _GENERATED.glob("*.py") if path.name != "__init__.py"}
    assert rendered == on_disk
