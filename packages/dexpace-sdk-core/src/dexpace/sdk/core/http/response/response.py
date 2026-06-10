# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Immutable HTTP response model."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import TYPE_CHECKING, Self

from ..common.headers import Headers
from ..common.http_header_name import HttpHeaderName
from ..common.protocol import Protocol
from .status import Status

if TYPE_CHECKING:
    from ..request.request import Request
    from .response_body import ResponseBody

type _Name = str | HttpHeaderName


@dataclass(frozen=True, slots=True)
class Response:
    """Immutable HTTP response produced by a transport.

    Implements the context-manager protocol so callers can

    .. code-block:: python

        with http_client.execute(request) as response:
            ...

    and the underlying body is closed deterministically on exit. The header /
    metadata surface is immutable and safe to share across threads; the body,
    when present, carries single-use stream state — see `ResponseBody`.
    """

    request: Request
    protocol: Protocol
    status: Status
    headers: Headers = field(default_factory=Headers)
    reason: str | None = None
    body: ResponseBody | None = None

    def close(self) -> None:
        """Close the response body. Idempotent."""
        if self.body is not None:
            self.body.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

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

    def with_body(self, body: ResponseBody | None) -> Self:
        return replace(self, body=body)

    def with_header(self, name: _Name, value: str) -> Self:
        return replace(self, headers=self.headers.with_set(name, value))


__all__ = ["Response"]
