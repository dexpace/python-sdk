# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Immutable async-aware HTTP response model."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import TYPE_CHECKING, Self

from .._shielded import _shielded_cleanup
from ..common.headers import Headers
from ..common.http_header_name import HttpHeaderName
from ..common.protocol import Protocol
from .status import Status

if TYPE_CHECKING:
    from ..request.request import Request
    from .async_response_body import AsyncResponseBody

type _Name = str | HttpHeaderName


@dataclass(frozen=True, slots=True)
class AsyncResponse:
    """Async twin of ``Response``.

    Implements the async context-manager protocol so the body is released
    deterministically on ``__aexit__``. All other semantics mirror
    ``Response`` exactly.
    """

    request: Request
    protocol: Protocol
    status: Status
    headers: Headers = field(default_factory=Headers)
    reason: str | None = None
    body: AsyncResponseBody | None = None

    async def close(self) -> None:
        """Close the response body. Idempotent.

        When invoked from ``__aexit__`` while an ``asyncio.CancelledError`` is
        propagating out of an ``async with`` block, the body close is shielded
        so the transport handle is released before cancellation continues.
        """
        if self.body is not None:
            await _shielded_cleanup(self.body.close())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def is_success(self) -> bool:
        return self.status.is_success

    @property
    def is_redirect(self) -> bool:
        return self.status.is_redirect

    @property
    def is_client_error(self) -> bool:
        return self.status.is_client_error

    @property
    def is_server_error(self) -> bool:
        return self.status.is_server_error

    def with_status(self, status: Status) -> Self:
        return replace(self, status=status)

    def with_headers(self, headers: Headers) -> Self:
        return replace(self, headers=headers)

    def with_body(self, body: AsyncResponseBody | None) -> Self:
        return replace(self, body=body)

    def with_header(self, name: _Name, value: str) -> Self:
        return replace(self, headers=self.headers.with_set(name, value))


__all__ = ["AsyncResponse"]
