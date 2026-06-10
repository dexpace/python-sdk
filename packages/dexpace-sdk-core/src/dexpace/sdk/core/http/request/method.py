# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""HTTP request methods."""

from __future__ import annotations

from enum import StrEnum


class Method(StrEnum):
    """HTTP request methods recognized by the SDK.

    `enum.StrEnum` (3.11+) gives string-valued members whose ``str()``
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
