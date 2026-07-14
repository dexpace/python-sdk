# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Synchronous petstore facade — generated; do not edit.

Rendered from the frozen OpenAPI document by ``examples/petstore/generate.py``.
A projection-only wrapper: every method binds its arguments into an
``OperationInput`` and delegates to the shared ``ServiceCore`` executor,
carrying no logic of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from dexpace.sdk.core.codegen import OperationInput

from .._support import PET_EVENT_MAPPER, PET_PAGE_STRATEGY, json_body
from ..models import Pet
from . import operations

if TYPE_CHECKING:
    from types import TracebackType

    from dexpace.sdk.core.codegen import ServiceCore
    from dexpace.sdk.core.http.sse.typed import TypedSseConnection
    from dexpace.sdk.core.pagination import Paginator

    from ..models import PetEvent, PetPatch

__all__ = ["PetStoreClient"]


class PetStoreClient:
    """Synchronous petstore client — a projection over ``ServiceCore``."""

    __slots__ = ("_core",)

    def __init__(self, core: ServiceCore) -> None:
        self._core = core

    def get_pet(self, pet_id: str) -> Pet:
        return self._core.execute(
            operations.GET_PET,
            OperationInput(path_params={"pet_id": pet_id}),
            response_type=Pet,
        )

    def list_pets(self, *, max_pages: int | None = None) -> Paginator[Pet]:
        return self._core.paginate(
            operations.LIST_PETS,
            OperationInput(),
            PET_PAGE_STRATEGY,
            max_pages=max_pages,
        )

    def update_pet(self, pet_id: str, patch: PetPatch) -> Pet:
        return self._core.execute(
            operations.UPDATE_PET,
            OperationInput(path_params={"pet_id": pet_id}, body=json_body(patch)),
            response_type=Pet,
        )

    def watch_pets(self) -> TypedSseConnection[PetEvent]:
        return self._core.events(
            operations.WATCH_PETS,
            OperationInput(),
            PET_EVENT_MAPPER,
        )

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
