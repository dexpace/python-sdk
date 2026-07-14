# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Generated petstore SDK — the standing codegen canary.

This package is produced from a frozen OpenAPI document (``spec/``) by the
deterministic generator (``generate.py``). The `_generated` subpackage holds the
regen-checked output — one operation table and two projection-only facades. The
hand-authored siblings (`models`, `errors`, `_support`) are the small runtime
shim a generated SDK ships alongside its facades: typed models, the status →
typed-error map, and the body / pagination / SSE binders the facades reference.

Nothing here is hand-edited after generation; a CI-style regen-diff test detects
any drift between the checked-in `_generated` files and a fresh generator run.
"""

from __future__ import annotations

from ._generated.async_client import AsyncPetStoreClient
from ._generated.sync_client import PetStoreClient
from .errors import PETSTORE_ERRORS, PetNotFoundError, PetStoreError
from .models import Pet, PetEvent, PetPatch

__all__ = [
    "PETSTORE_ERRORS",
    "AsyncPetStoreClient",
    "Pet",
    "PetEvent",
    "PetNotFoundError",
    "PetPatch",
    "PetStoreClient",
    "PetStoreError",
]
