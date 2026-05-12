"""Immutable HTTP request model."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Self

from ..common.headers import Headers
from ..common.http_header_name import HttpHeaderName
from .method import Method

if TYPE_CHECKING:
    from .request_body import RequestBody

type _Name = str | HttpHeaderName


@dataclass(frozen=True, slots=True)
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
    body: RequestBody | None = None

    def with_method(self, method: Method) -> Self:
        return replace(self, method=method)

    def with_url(self, url: str) -> Self:
        return replace(self, url=url)

    def with_headers(self, headers: Headers) -> Self:
        return replace(self, headers=headers)

    def with_body(self, body: RequestBody | None) -> Self:
        return replace(self, body=body)

    def with_header(self, name: _Name, value: str) -> Self:
        """Replace any existing values for ``name`` with the single ``value``."""
        return replace(self, headers=self.headers.with_set(name, value))

    def with_added_header(self, name: _Name, value: str) -> Self:
        """Append ``value`` to ``name``'s existing values."""
        return replace(self, headers=self.headers.with_added(name, value))

    def without_header(self, name: _Name) -> Self:
        return replace(self, headers=self.headers.without(name))


__all__ = ["Request"]
