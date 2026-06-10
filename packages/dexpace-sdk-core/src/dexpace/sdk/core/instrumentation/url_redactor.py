# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Redact sensitive components from URLs before log emission."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from ..http.common.url import QueryParams, Url

#: Default allow-list of query parameters that pass through unredacted.
#: Matches the dexpace/java-sdk default and is intentionally conservative.
DEFAULT_QUERY_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "api-version",
        "comp",
        "encoding",
        "fields",
        "filter",
        "include",
        "limit",
        "offset",
        "order",
        "orderby",
        "page",
        "page_size",
        "search",
        "select",
        "skip",
        "sort",
        "top",
        "view",
    }
)

_REDACTED: Final[str] = "REDACTED"
_REDACTED_PATH: Final[str] = "/REDACTED"


class UrlRedactor:
    """Strip userinfo and non-allowlisted query parameters from a URL.

    Used by logging policies to emit URLs without leaking credentials,
    tokens, or PII embedded in query strings. The redactor returns a
    ``str`` (not a ``Url``) so callers can format it directly.

    Attributes:
        allowed_query_keys: Query parameter names emitted unredacted.
        redact_path: When ``True``, replace the path with ``/REDACTED``.
            Defaults to ``False`` because paths are commonly useful in
            logs and rarely carry secrets.
        redact_fragment: When ``True`` (the default), drop the URL
            fragment entirely. Fragments are not normally sent on the
            wire but may appear in logged input; bearer tokens are
            sometimes carried there (e.g. OAuth implicit flow).
    """

    __slots__ = ("allowed_query_keys", "redact_fragment", "redact_path")

    def __init__(
        self,
        allowed_query_keys: Iterable[str] = DEFAULT_QUERY_ALLOWLIST,
        *,
        redact_path: bool = False,
        redact_fragment: bool = True,
    ) -> None:
        self.allowed_query_keys = frozenset(allowed_query_keys)
        self.redact_path = redact_path
        self.redact_fragment = redact_fragment

    def redact(self, url: str | Url) -> str:
        """Return a redacted wire-form string for ``url``.

        Args:
            url: Either a parsed ``Url`` or a wire-form string. Strings are
                parsed via ``Url.parse``; parse failures fall through to
                returning the input unchanged (so logging never silently
                drops a URL because it's malformed).

        Returns:
            A wire-form URL with userinfo stripped and each non-allowlisted
            parameter collapsed to ``REDACTED=REDACTED`` (both key and value),
            so neither the secret nor the parameter name leaks.
        """
        parsed = url if isinstance(url, Url) else _safe_parse(url)
        if parsed is None:
            return str(url)
        return str(self._redact_parsed(parsed))

    def _redact_parsed(self, parsed: Url) -> Url:
        redacted_query = QueryParams(
            [
                (key, value) if key in self.allowed_query_keys else (_REDACTED, _REDACTED)
                for key, values in parsed.query.items()
                for value in values
            ]
        )
        path = _REDACTED_PATH if self.redact_path else parsed.path
        fragment = "" if self.redact_fragment else parsed.fragment
        return Url(
            scheme=parsed.scheme,
            host=parsed.host,
            path=path,
            port=parsed.port,
            query=redacted_query,
            fragment=fragment,
            userinfo=None,
        )


def _safe_parse(raw: str) -> Url | None:
    try:
        return Url.parse(raw)
    except ValueError:
        return None


__all__ = ["DEFAULT_QUERY_ALLOWLIST", "UrlRedactor"]
