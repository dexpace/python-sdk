# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Operation table for the petstore canary — generated; do not edit.

Rendered from the frozen OpenAPI document by ``examples/petstore/generate.py``.
Pure data: one ``Operation`` declaration per API operation, imported verbatim
by both the sync and async facades.
"""

from __future__ import annotations

from dexpace.sdk.core.codegen import AuthDescriptor, AuthRequirement, Operation
from dexpace.sdk.core.http.request import Method

__all__ = ["GET_PET", "LIST_PETS", "UPDATE_PET", "WATCH_PETS"]

_GET_PET_AUTH = AuthDescriptor.of(AuthRequirement("bearer", ("pets:read",)))

GET_PET = Operation(name="get_pet", method=Method.GET, path="/pets/{pet_id}", auth=_GET_PET_AUTH)
LIST_PETS = Operation(name="list_pets", method=Method.GET, path="/pets")
UPDATE_PET = Operation(name="update_pet", method=Method.PATCH, path="/pets/{pet_id}")
WATCH_PETS = Operation(name="watch_pets", method=Method.GET, path="/pets/events")
