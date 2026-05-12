"""HTTP request methods."""
from __future__ import annotations

from enum import Enum


class Method(str, Enum):
    """HTTP request methods recognized by the SDK.

    Subclasses ``str`` so callers can interpolate values directly into a
    request line and compare against plain strings: ``Method.GET == "GET"``.
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

    def __str__(self) -> str:
        return self.value


__all__ = ["Method"]
