# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Asynchronous petstore facade — generated; do not edit.

The async twin of the sync facade. Rendered from the same method template by
``examples/petstore/generate.py`` with the async mode switch, so it normalises
to the sync facade's AST under the parity gate. A projection-only wrapper over
the shared ``AsyncServiceCore`` executor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from dexpace.sdk.core.codegen import OperationInput

from .._support import PET_EVENT_MAPPER, PET_PAGE_STRATEGY, json_body
from ..models import Pet
from . import operations

if TYPE_CHECKING:
    from types import TracebackType

    from dexpace.sdk.core.codegen import AsyncServiceCore
    from dexpace.sdk.core.http.sse.typed import AsyncTypedSseConnection
    from dexpace.sdk.core.pagination import AsyncPaginator

    from ..models import PetEvent, PetPatch

__all__ = ["AsyncPetStoreClient"]


class AsyncPetStoreClient:
    """Asynchronous petstore client — a projection over ``AsyncServiceCore``."""

    __slots__ = ("_core",)

    def __init__(self, core: AsyncServiceCore) -> None:
        self._core = core

    async def get_pet(self, pet_id: str) -> Pet:
        return await self._core.execute(
            operations.GET_PET,
            OperationInput(path_params={"pet_id": pet_id}),
            response_type=Pet,
        )

    def list_pets(self, *, max_pages: int | None = None) -> AsyncPaginator[Pet]:
        return self._core.paginate(
            operations.LIST_PETS,
            OperationInput(),
            PET_PAGE_STRATEGY,
            max_pages=max_pages,
        )

    async def update_pet(self, pet_id: str, patch: PetPatch) -> Pet:
        return await self._core.execute(
            operations.UPDATE_PET,
            OperationInput(path_params={"pet_id": pet_id}, body=json_body(patch)),
            response_type=Pet,
        )

    def watch_pets(self) -> AsyncTypedSseConnection[PetEvent]:
        return self._core.events(
            operations.WATCH_PETS,
            OperationInput(),
            PET_EVENT_MAPPER,
        )

    async def aclose(self) -> None:
        await self._core.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
