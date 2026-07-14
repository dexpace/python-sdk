# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Immutable HTTP request model."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, Self

from ..common.headers import Headers
from ..common.http_header_name import HttpHeaderName
from ..common.url import Url
from .method import Method

if TYPE_CHECKING:
    from .request_body import RequestBody

type _Name = str | HttpHeaderName

#: Methods whose semantics forbid a request payload body. Constructing a request
#: for one of these with a body is rejected; ``with_method`` drops an existing
#: body when switching to one of them so fluent chains stay valid.
_BODYLESS_METHODS: Final[frozenset[Method]] = frozenset({Method.GET, Method.HEAD})


@dataclass(frozen=True, slots=True)
class Request:
    """Immutable HTTP request handed to a transport.

    Construct directly via the dataclass constructor or derive a new instance
    non-destructively via `dataclasses.replace` or the ``with_*`` helpers.
    Every construction path (including ``replace``) re-runs ``__post_init__``,
    so the validated invariants below always hold.

    Header / metadata surface is immutable and safe to share across threads;
    the ``body``, when present, carries single-use stream state — clone before
    sharing if you retain a stream-backed body. See `RequestBody`.

    Attributes:
        method: The HTTP method.
        url: The absolute request target.
        headers: The request headers (an immutable, case-insensitive value).
        body: The optional request payload. Forbidden for ``GET`` / ``HEAD``.
        timeout: Optional per-call timeout in seconds. Must be positive.
        retries: Optional per-call retry budget. ``0`` disables retries for
            this call; a negative value is rejected.
    """

    method: Method
    url: Url
    headers: Headers = field(default_factory=Headers)
    body: RequestBody | None = None
    timeout: float | None = None
    retries: int | None = None

    def __post_init__(self) -> None:
        """Validate required fields, the body/method pairing, and per-call bounds.

        Raises:
            ValueError: If ``method`` or ``url`` is missing, if a body is
                supplied for a method that forbids one (``GET`` / ``HEAD``), if
                ``timeout`` is not positive, or if ``retries`` is negative. The
                message names the offending field.
        """
        if self.method is None:
            raise ValueError("method is required")
        if self.url is None:
            raise ValueError("url is required")
        if self.body is not None and self.method in _BODYLESS_METHODS:
            raise ValueError(f"body is not permitted on a {self.method} request")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.retries is not None and self.retries < 0:
            raise ValueError("retries must not be negative")

    def __repr__(self) -> str:
        """Return a repr with the URL redacted via the shared ``UrlRedactor``.

        The default dataclass repr would render the ``Url`` in full, leaking any
        userinfo or query-string secrets into logs and tracebacks. Routing the
        URL through the one shared redactor — and rendering ``headers`` through
        `Headers` (which masks credential-bearing header values) — keeps that
        surface consistent with the logging and tracing emitters and ensures no
        credential is ever echoed.
        """
        from ...instrumentation.url_redactor import UrlRedactor

        url = UrlRedactor().redact(self.url)
        return (
            f"Request(method={self.method!r}, url={url!r}, "
            f"headers={self.headers!r}, body={self.body!r}, "
            f"timeout={self.timeout!r}, retries={self.retries!r})"
        )

    def __repr__(self) -> str:
        """Return a repr with the URL redacted via the shared ``UrlRedactor``.

        The default dataclass repr would render the ``Url`` in full, leaking any
        userinfo or query-string secrets into logs and tracebacks. Routing the
        URL through the one shared redactor keeps that surface consistent with
        the logging and tracing emitters.
        """
        from ...instrumentation.url_redactor import UrlRedactor

        url = UrlRedactor().redact(self.url)
        return (
            f"Request(method={self.method!r}, url={url!r}, "
            f"headers={self.headers!r}, body={self.body!r})"
        )

    def with_method(self, method: Method) -> Self:
        """Return a copy with ``method`` replaced.

        Switching to a body-less method (``GET`` / ``HEAD``) drops any existing
        body, because that combination is invalid; every other switch leaves the
        body untouched. Dropping here keeps fluent chains such as a redirect's
        ``with_method(GET).with_body(None)`` valid on the intermediate hop.
        """
        if method in _BODYLESS_METHODS and self.body is not None:
            return replace(self, method=method, body=None)
        return replace(self, method=method)

    def with_url(self, url: str | Url) -> Self:
        parsed = Url.parse(url) if isinstance(url, str) else url
        return replace(self, url=parsed)

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
