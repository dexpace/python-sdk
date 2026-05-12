"""Immutable HTTP response model."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Optional

from ..common.headers import Headers
from ..common.protocol import Protocol
from .status import Status

if TYPE_CHECKING:
    from ..request.request import Request
    from .response_body import ResponseBody


@dataclass(frozen=True)
class Response:
    """Immutable HTTP response produced by a transport.

    Implements the context-manager protocol so callers can

    .. code-block:: python

        with http_client.execute(request) as response:
            ...

    and the underlying body is closed deterministically on exit. The header /
    metadata surface is immutable and safe to share across threads; the body,
    when present, carries single-use stream state — see :class:`ResponseBody`.
    """

    request: "Request"
    protocol: Protocol
    status: Status
    headers: Headers = field(default_factory=Headers)
    message: Optional[str] = None
    body: Optional["ResponseBody"] = None

    def close(self) -> None:
        """Close the response body. Idempotent."""
        if self.body is not None:
            self.body.close()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def is_success(self) -> bool:
        return self.status.is_success

    def with_status(self, status: Status) -> "Response":
        return replace(self, status=status)

    def with_headers(self, headers: Headers) -> "Response":
        return replace(self, headers=headers)

    def with_body(self, body: Optional["ResponseBody"]) -> "Response":
        return replace(self, body=body)

    def with_header(self, name: str, value: str) -> "Response":
        return replace(self, headers=self.headers.with_set(name, value))


__all__ = ["Response"]
