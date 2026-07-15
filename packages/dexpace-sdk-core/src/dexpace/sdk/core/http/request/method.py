# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""HTTP request methods."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Method(StrEnum):
    """HTTP request methods recognized by the SDK.

    `enum.StrEnum` (3.11+) gives string-valued members whose ``str()``
    *is* the wire form, so callers can interpolate or compare against bare
    strings: ``Method.GET == "GET"``. The member name and its value are always
    identical, so the enum doubles as the canonical wire token — usable
    verbatim in a request line without any translation step.
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


#: The idempotent HTTP methods (RFC 7231 §4.2.2): a repeated request has the
#: same effect on server state as a single one. This is the sole idempotency
#: classification the SDK recognises — there is no separate "safe method" set —
#: and it is the single source that retry logic derives from: the default retry
#: method allow-list and the inherent replay-safety gate both flow from it, so a
#: request is only ever re-sent on a transport error when its method is here.
#: Kept as an internal module constant rather than a public accessor on
#: `Method`, so the classification stays an implementation detail of retry.
_IDEMPOTENT_METHODS: Final[frozenset[Method]] = frozenset(
    {Method.GET, Method.HEAD, Method.OPTIONS, Method.PUT, Method.DELETE}
)


__all__ = ["Method"]
