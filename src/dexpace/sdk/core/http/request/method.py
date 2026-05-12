"""HTTP request methods."""
from __future__ import annotations

from enum import StrEnum


class Method(StrEnum):
    """HTTP request methods recognized by the SDK.

    :class:`enum.StrEnum` (3.11+) gives string-valued members whose ``str()``
    *is* the wire form, so callers can interpolate or compare against bare
    strings: ``Method.GET == "GET"``.
    """

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    TRACE = "TRACE"
    CONNECT = "CONNECT"


__all__ = ["Method"]
