"""Typed constants for well-known HTTP header names."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class HttpHeaderName:
    """Wire-form name of a well-known HTTP header.

    Stores both the lower-case form used internally by ``Headers`` for
    case-insensitive lookup and the canonical mixed-case form preferred for
    log emission and over-the-wire serialization. Equality and hashing key on
    ``value`` (the lower-case form), so two ``HttpHeaderName`` instances
    built from differing casings of the same header compare equal.

    Pass instances directly to ``Headers.get`` and friends — the header APIs
    treat them interchangeably with ``str``, saving the lookup-time
    ``.lower()`` call.

    Attributes:
        value: Lower-case form (e.g. ``content-type``). Used for lookup.
        canonical_name: Mixed-case wire form (e.g. ``Content-Type``).
    """

    value: str
    canonical_name: str

    @classmethod
    def of(cls, canonical_name: str) -> Self:
        """Build from a canonical name, deriving the lower-case form.

        Args:
            canonical_name: Mixed-case wire form (e.g. ``X-Trace-Id``).

        Returns:
            A new ``HttpHeaderName`` whose ``value`` is the lower-cased name.
        """
        return cls(canonical_name.lower(), canonical_name)

    def __str__(self) -> str:
        return self.canonical_name


def _name(canonical: str) -> HttpHeaderName:
    return HttpHeaderName(canonical.lower(), canonical)


# Standard request headers.
ACCEPT = _name("Accept")
ACCEPT_CHARSET = _name("Accept-Charset")
ACCEPT_ENCODING = _name("Accept-Encoding")
ACCEPT_LANGUAGE = _name("Accept-Language")
ACCEPT_RANGES = _name("Accept-Ranges")
AUTHORIZATION = _name("Authorization")
CACHE_CONTROL = _name("Cache-Control")
CONNECTION = _name("Connection")
CONTENT_DISPOSITION = _name("Content-Disposition")
CONTENT_ENCODING = _name("Content-Encoding")
CONTENT_LANGUAGE = _name("Content-Language")
CONTENT_LENGTH = _name("Content-Length")
CONTENT_LOCATION = _name("Content-Location")
CONTENT_MD5 = _name("Content-MD5")
CONTENT_RANGE = _name("Content-Range")
CONTENT_TYPE = _name("Content-Type")
COOKIE = _name("Cookie")
DATE = _name("Date")
ETAG = _name("ETag")
EXPECT = _name("Expect")
EXPIRES = _name("Expires")
FROM = _name("From")
HOST = _name("Host")
IF_MATCH = _name("If-Match")
IF_MODIFIED_SINCE = _name("If-Modified-Since")
IF_NONE_MATCH = _name("If-None-Match")
IF_RANGE = _name("If-Range")
IF_UNMODIFIED_SINCE = _name("If-Unmodified-Since")
LAST_MODIFIED = _name("Last-Modified")
LOCATION = _name("Location")
MAX_FORWARDS = _name("Max-Forwards")
ORIGIN = _name("Origin")
PRAGMA = _name("Pragma")
PROXY_AUTHENTICATE = _name("Proxy-Authenticate")
PROXY_AUTHORIZATION = _name("Proxy-Authorization")
RANGE = _name("Range")
REFERER = _name("Referer")
RETRY_AFTER = _name("Retry-After")
SERVER = _name("Server")
SET_COOKIE = _name("Set-Cookie")
TE = _name("TE")
TRAILER = _name("Trailer")
TRANSFER_ENCODING = _name("Transfer-Encoding")
UPGRADE = _name("Upgrade")
USER_AGENT = _name("User-Agent")
VARY = _name("Vary")
VIA = _name("Via")
WARNING = _name("Warning")
WWW_AUTHENTICATE = _name("WWW-Authenticate")

# Common SDK-flavoured headers.
X_FORWARDED_FOR = _name("X-Forwarded-For")
X_FORWARDED_HOST = _name("X-Forwarded-Host")
X_FORWARDED_PROTO = _name("X-Forwarded-Proto")
X_REQUEST_ID = _name("X-Request-Id")
TRACEPARENT = _name("traceparent")
TRACESTATE = _name("tracestate")


__all__ = [
    "ACCEPT",
    "ACCEPT_CHARSET",
    "ACCEPT_ENCODING",
    "ACCEPT_LANGUAGE",
    "ACCEPT_RANGES",
    "AUTHORIZATION",
    "CACHE_CONTROL",
    "CONNECTION",
    "CONTENT_DISPOSITION",
    "CONTENT_ENCODING",
    "CONTENT_LANGUAGE",
    "CONTENT_LENGTH",
    "CONTENT_LOCATION",
    "CONTENT_MD5",
    "CONTENT_RANGE",
    "CONTENT_TYPE",
    "COOKIE",
    "DATE",
    "ETAG",
    "EXPECT",
    "EXPIRES",
    "FROM",
    "HOST",
    "IF_MATCH",
    "IF_MODIFIED_SINCE",
    "IF_NONE_MATCH",
    "IF_RANGE",
    "IF_UNMODIFIED_SINCE",
    "LAST_MODIFIED",
    "LOCATION",
    "MAX_FORWARDS",
    "ORIGIN",
    "PRAGMA",
    "PROXY_AUTHENTICATE",
    "PROXY_AUTHORIZATION",
    "RANGE",
    "REFERER",
    "RETRY_AFTER",
    "SERVER",
    "SET_COOKIE",
    "TE",
    "TRACEPARENT",
    "TRACESTATE",
    "TRAILER",
    "TRANSFER_ENCODING",
    "UPGRADE",
    "USER_AGENT",
    "VARY",
    "VIA",
    "WARNING",
    "WWW_AUTHENTICATE",
    "X_FORWARDED_FOR",
    "X_FORWARDED_HOST",
    "X_FORWARDED_PROTO",
    "X_REQUEST_ID",
    "HttpHeaderName",
]
