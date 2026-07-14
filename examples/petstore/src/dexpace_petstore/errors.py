# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Typed error taxonomy for the petstore canary.

`PETSTORE_ERRORS` is the `StatusErrorMap` the executor consults on a non-success
response: a 404 raises `PetNotFoundError`, and every other error status raises
the `PetStoreError` base. The service errors sit purely on the response branch
(they extend `HttpResponseError`, never `OSError`), so an ``except OSError:`` site
still only catches transport failures.
"""

from __future__ import annotations

from dexpace.sdk.core.codegen import StatusErrorMap
from dexpace.sdk.core.errors import HttpResponseError
from dexpace.sdk.core.http.response import Status

from .models import Pet

__all__ = ["PETSTORE_ERRORS", "PetNotFoundError", "PetStoreError"]


class PetStoreError(HttpResponseError[Pet]):
    """Base class for every typed petstore response error."""


class PetNotFoundError(PetStoreError):
    """Raised for a 404 — no pet matched the requested id."""


PETSTORE_ERRORS: StatusErrorMap = StatusErrorMap(
    by_status={Status.NOT_FOUND: PetNotFoundError},
    default=PetStoreError,
)
