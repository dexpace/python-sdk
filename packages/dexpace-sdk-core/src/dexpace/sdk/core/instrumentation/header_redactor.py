# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Decide which header names are logged and redact URL-valued header values.

This is the one component shared by the sync and async logging policies for
header-value redaction, so the rules cannot drift between the two pillars.
Header logging is default-deny by name: only allow-listed names have their
value logged, URL-valued headers (``Location`` / ``Content-Location``) route
their value through the shared `UrlRedactor`, and a disallowed name is either
marked ``REDACTED`` or omitted entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .url_redactor import UrlRedactor

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..http.common.headers import Headers

#: Value substituted for a header whose name is not allow-listed (when the
#: policy marks rather than omits it). It replaces the value, never the name,
#: so a disallowed header's value is never logged.
_REDACTED_MARKER: Final[str] = "REDACTED"

#: Default allow-list of header NAMES whose value may be logged. Deliberately
#: conservative: only diagnostic, non-credential headers. Credential-bearing
#: headers (``authorization``, ``proxy-authorization``, ``cookie``,
#: ``set-cookie``, ``www-authenticate``, ``proxy-authenticate``, API-key
#: headers) are intentionally absent so their value never reaches the log.
DEFAULT_LOGGED_HEADER_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "accept",
        "accept-charset",
        "accept-encoding",
        "accept-language",
        "accept-ranges",
        "age",
        "cache-control",
        "connection",
        "content-encoding",
        "content-language",
        "content-length",
        "content-location",
        "content-range",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "location",
        "pragma",
        "retry-after",
        "server",
        "trailer",
        "transfer-encoding",
        "user-agent",
        "vary",
        "via",
        "warning",
        "traceparent",
        "tracestate",
        "x-correlation-id",
        "x-request-id",
    }
)

#: Header names whose value is a URL and must be redacted through the URL-value
#: redactor before logging. Location and Content-Location are the minimum.
DEFAULT_URL_VALUED_HEADERS: Final[frozenset[str]] = frozenset({"content-location", "location"})


class HeaderRedactor:
    """Gate header names by an allow-list and redact URL-valued header values.

    Attributes:
        allowed_names: Lower-cased header names whose value may be logged.
        url_valued_names: Lower-cased header names whose value is a URL and is
            routed through the shared `UrlRedactor`.
        redact_disallowed: When ``True`` (the default) a name that is not
            allow-listed is emitted with the fixed ``"REDACTED"`` marker in
            place of its value; when ``False`` it is omitted entirely.
    """

    __slots__ = ("_url_redactor", "allowed_names", "redact_disallowed", "url_valued_names")

    def __init__(
        self,
        *,
        allowed_names: Iterable[str] = DEFAULT_LOGGED_HEADER_ALLOWLIST,
        url_valued_names: Iterable[str] = DEFAULT_URL_VALUED_HEADERS,
        url_redactor: UrlRedactor | None = None,
        redact_disallowed: bool = True,
    ) -> None:
        """Configure the redactor.

        Args:
            allowed_names: Header names whose value may be logged. Compared
                case-insensitively.
            url_valued_names: Header names whose value is a URL. Compared
                case-insensitively; a name here should also be allow-listed.
            url_redactor: Redactor applied to URL-valued header values. Defaults
                to a stock `UrlRedactor`.
            redact_disallowed: Whether a disallowed name is marked ``REDACTED``
                (``True``) or omitted (``False``).
        """
        self.allowed_names = frozenset(name.lower() for name in allowed_names)
        self.url_valued_names = frozenset(name.lower() for name in url_valued_names)
        self._url_redactor = url_redactor or UrlRedactor()
        self.redact_disallowed = redact_disallowed

    def redact(self, headers: Headers) -> dict[str, str]:
        """Return the loggable ``name -> value`` map for ``headers``.

        Header names arrive already lower-cased from `Headers`. Multi-valued
        headers are joined with ``", "`` (the HTTP list convention). An
        allow-listed URL-valued header is redacted through the URL-value
        redactor; any other allow-listed header passes through unchanged. A
        name that is not allow-listed is marked ``REDACTED`` or omitted per
        `redact_disallowed`.

        Args:
            headers: The header collection to filter and redact.

        Returns:
            A mapping of the header names that may be logged to their (possibly
            redacted) values.
        """
        result: dict[str, str] = {}
        for name, values in headers.items():
            if name in self.allowed_names:
                joined = ", ".join(values)
                result[name] = (
                    self._url_redactor.redact_header_value(joined)
                    if name in self.url_valued_names
                    else joined
                )
            elif self.redact_disallowed:
                result[name] = _REDACTED_MARKER
        return result


__all__ = [
    "DEFAULT_LOGGED_HEADER_ALLOWLIST",
    "DEFAULT_URL_VALUED_HEADERS",
    "HeaderRedactor",
]
