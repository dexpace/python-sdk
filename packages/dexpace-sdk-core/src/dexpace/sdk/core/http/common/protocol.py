# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""HTTP protocol versions describable on a `Response`."""

from __future__ import annotations

from enum import StrEnum
from typing import Self


class Protocol(StrEnum):
    """Wire-protocol identifier negotiated for an HTTP exchange.

    The wire form (returned by ``__str__`` and consumed by `parse`) is
    lower-case with a slash separator, matching ALPN identifiers.
    """

    HTTP_1_0 = "http/1.0"
    HTTP_1_1 = "http/1.1"
    HTTP_2 = "http/2"
    H2_PRIOR_KNOWLEDGE = "h2_prior_knowledge"
    QUIC = "quic"

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse a protocol identifier case-insensitively.

        Accepts the canonical forms emitted by ``__str__`` plus the alternative
        spellings ``HTTP/2`` and ``HTTP/2.0`` for HTTP/2.

        Raises:
            ValueError: if ``value`` does not match a known protocol.
        """
        normalized = value.lower()
        if normalized in ("http/2.0", "h2"):
            return cls.HTTP_2
        for proto in cls:
            if proto.value == normalized:
                return proto
        raise ValueError(f"Unknown protocol: {value!r}")


__all__ = ["Protocol"]
