# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Redact sensitive components from URLs before log emission.

This is the one default-deny URL redactor shared by every emitter that renders
a URL for diagnostics: the request/response ``__repr__``, the logging policy,
and the tracing span attributes. Keeping the logic in a single place means a
credential, token, or PII value is stripped the same way no matter which sink
surfaces it.

Parsing here does NOT route through ``furl`` (the throwing parser behind
``Url.parse``). Instead ``redact`` uses a total, never-raising splitter so a
malformed string can never crash the logging path; it collapses to the literal
``"[malformed url]"`` placeholder instead of leaking a possibly secret-bearing
input verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
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
_REDACTED_PAIR: Final[str] = "REDACTED=REDACTED"
_REDACTED_PATH: Final[str] = "/REDACTED"

#: Returned in place of a URL that cannot be parsed. Failing closed to a fixed
#: string keeps an unparseable, possibly secret-bearing input out of the logs.
_MALFORMED: Final[str] = "[malformed url]"

#: A scheme is ``ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )`` per RFC 3986 §3.1.
#: Anything else before ``://`` is treated as malformed rather than a scheme.
_SCHEME_RE: Final[re.Pattern[str]] = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*")

_MAX_PORT: Final[int] = 65535


@dataclass(frozen=True, slots=True)
class _Parts:
    """The redaction-relevant components of a URL.

    Userinfo is intentionally absent: it is dropped during splitting and never
    reconstructed, so embedded credentials cannot survive a round trip.
    """

    scheme: str
    host: str
    port: int | None
    path: str
    query: QueryParams
    fragment: str


class UrlRedactor:
    """Strip userinfo and non-allowlisted query parameters from a URL.

    Used by logging, tracing, and repr emitters to render URLs without leaking
    credentials, tokens, or PII. The redactor returns a ``str`` (not a ``Url``)
    so callers can format it directly.

    Attributes:
        allowed_query_keys: Query parameter names emitted unredacted. The
            decision is per key and atomic: every value of an allow-listed key
            survives, every value of any other key collapses to
            ``REDACTED=REDACTED``.
        redact_path: When ``True``, replace the path with ``/REDACTED``.
            Defaults to ``False`` because paths are commonly useful in logs and
            rarely carry secrets.
        redact_fragment: When ``True`` (the default), scrub ``key=value`` pairs
            in the fragment (bearer tokens are sometimes carried there, e.g. the
            OAuth implicit flow) while preserving a plain, non-key/value
            fragment as-is. When ``False``, the fragment is emitted verbatim.
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
            url: Either an already-parsed ``Url`` or a wire-form string. Strings
                are split by a total, never-raising parser; a string that cannot
                be split into at least a scheme and host yields the constant
                ``"[malformed url]"`` rather than the input, so a malformed
                string that may embed a secret never reaches the log sink.

        Returns:
            A wire-form URL with userinfo stripped, each non-allowlisted query
            parameter collapsed to ``REDACTED=REDACTED`` (both key and value, so
            neither the secret nor the parameter name leaks), and fragment
            ``key=value`` pairs scrubbed. Returns ``"[malformed url]"`` when a
            string input cannot be parsed. This method never raises.
        """
        parts = _parts_from_url(url) if isinstance(url, Url) else _split(url)
        if parts is None:
            return _MALFORMED
        return self._render(parts)

    def _render(self, parts: _Parts) -> str:
        """Reassemble ``parts`` into a redacted wire-form string.

        The fragment is appended after the query is decided, so a literal ``?``
        living inside a fragment can never be mistaken for a query separator.
        """
        out = [parts.scheme, "://", parts.host]
        if parts.port is not None:
            out.append(f":{parts.port}")
        out.append(_REDACTED_PATH if self.redact_path else parts.path)
        query = self._redact_query(parts.query)
        if query:
            out.append(f"?{query}")
        fragment = self._redact_fragment(parts.fragment)
        if fragment:
            out.append(f"#{fragment}")
        return "".join(out)

    def _redact_query(self, query: QueryParams) -> str:
        """Redact non-allowlisted parameters and re-encode (no leading ``?``)."""
        redacted = QueryParams(
            [
                (key, value) if key in self.allowed_query_keys else (_REDACTED, _REDACTED)
                for key, values in query.items()
                for value in values
            ]
        )
        return redacted.encode()

    def _redact_fragment(self, fragment: str) -> str:
        """Scrub ``key=value`` fragment segments; preserve plain ones.

        When ``redact_fragment`` is disabled the fragment is returned verbatim.
        Otherwise each ``&``-separated segment containing ``=`` collapses to
        ``REDACTED=REDACTED`` (matching the query treatment) while a segment
        with no ``=`` — a plain anchor such as ``section-2`` — is kept as-is.
        """
        if not self.redact_fragment or not fragment:
            return fragment
        segments = fragment.split("&")
        return "&".join(_REDACTED_PAIR if "=" in segment else segment for segment in segments)


def _parts_from_url(url: Url) -> _Parts:
    """Project an already-parsed ``Url`` onto its redaction-relevant parts.

    The ``Url`` was produced by ``Url.parse`` upstream, so no re-parsing (and no
    ``furl`` call) happens here; userinfo is simply dropped.
    """
    return _Parts(
        scheme=url.scheme,
        host=url.host,
        port=url.port,
        path=url.path,
        query=url.query,
        fragment=url.fragment,
    )


def _split(raw: str) -> _Parts | None:
    """Split a wire-form string into ``_Parts`` without ever raising.

    A hand-rolled RFC-3986-shaped splitter used in place of ``furl`` (which
    raises on bad input). The fragment is peeled first and the query second, so
    a ``?`` inside a fragment never re-splits the query. Returns ``None`` when
    the input lacks a valid scheme or host, signalling a malformed URL.

    Args:
        raw: A wire-form URL string.

    Returns:
        The parsed ``_Parts``, or ``None`` if ``raw`` is malformed.
    """
    if not raw:
        return None
    before_fragment, _, fragment = raw.partition("#")
    before_query, _, query = before_fragment.partition("?")
    scheme, separator, after = before_query.partition("://")
    if not separator or not _SCHEME_RE.fullmatch(scheme):
        return None
    authority, path = _split_authority_path(after)
    host, port = _parse_host_port(_strip_userinfo(authority))
    if host is None:
        return None
    return _Parts(
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        query=QueryParams.parse(query),
        fragment=fragment,
    )


def _split_authority_path(after: str) -> tuple[str, str]:
    """Split ``host[:port]`` from the path at the first ``/`` (kept on the path)."""
    slash = after.find("/")
    if slash < 0:
        return after, ""
    return after[:slash], after[slash:]


def _strip_userinfo(authority: str) -> str:
    """Drop a ``user[:password]@`` prefix, splitting on the last ``@``."""
    at_sign = authority.rfind("@")
    return authority[at_sign + 1 :] if at_sign >= 0 else authority


def _parse_host_port(host_port: str) -> tuple[str | None, int | None]:
    """Split ``host[:port]`` (including bracketed IPv6) without raising.

    Returns ``(None, None)`` for a malformed authority — an empty host, an
    unterminated IPv6 literal, or a non-numeric/out-of-range port — so the
    caller can fail closed.
    """
    if host_port.startswith("["):
        close = host_port.find("]")
        if close < 0:
            return None, None
        host = host_port[: close + 1]
        remainder = host_port[close + 1 :]
        if len(host) <= 2:
            return None, None
        if not remainder:
            return host, None
        return _with_port(host, remainder[1:]) if remainder.startswith(":") else (None, None)
    host, separator, port = host_port.rpartition(":")
    if not separator:
        return (host_port, None) if host_port else (None, None)
    return _with_port(host, port) if host else (None, None)


def _with_port(host: str, port: str) -> tuple[str | None, int | None]:
    """Attach a validated numeric port, or fail closed on a bad one."""
    if not (port.isascii() and port.isdigit()):
        return None, None
    number = int(port)
    return (host, number) if number <= _MAX_PORT else (None, None)


__all__ = ["DEFAULT_QUERY_ALLOWLIST", "UrlRedactor"]
