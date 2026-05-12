"""Immutable HTTP request model."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Optional

from ..common.headers import Headers
from .method import Method

if TYPE_CHECKING:
    from .request_body import RequestBody


@dataclass(frozen=True)
class Request:
    """Immutable HTTP request handed to a transport.

    Construct directly via the dataclass constructor or derive a new instance
    non-destructively via :func:`dataclasses.replace` or the ``with_*`` helpers.

    Frozen dataclass — instances are safe to share across threads. The ``body``,
    when present, may carry single-use stream state; see :class:`RequestBody`.
    """

    method: Method
    url: str
    headers: Headers = field(default_factory=Headers)
    body: Optional["RequestBody"] = None

    def with_method(self, method: Method) -> "Request":
        return replace(self, method=method)

    def with_url(self, url: str) -> "Request":
        return replace(self, url=url)

    def with_headers(self, headers: Headers) -> "Request":
        return replace(self, headers=headers)

    def with_body(self, body: Optional["RequestBody"]) -> "Request":
        return replace(self, body=body)

    def with_header(self, name: str, value: str) -> "Request":
        """Replace any existing values for ``name`` with the single ``value``."""
        return replace(self, headers=self.headers.with_set(name, value))

    def with_added_header(self, name: str, value: str) -> "Request":
        """Append ``value`` to ``name``'s existing values."""
        return replace(self, headers=self.headers.with_added(name, value))

    def without_header(self, name: str) -> "Request":
        return replace(self, headers=self.headers.without(name))


__all__ = ["Request"]
