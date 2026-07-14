# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Pure retryability classifier: status table + cause-chain walk.

Single source of the "should this be retried?" decision, shared by the sync
and async retry policies and by ``HttpResponseError`` construction. The module
is stdlib-only and free of pipeline/error imports so those consumers import
the one definition rather than each restating it.

Two independent surfaces:

- ``status_is_retryable`` answers whether an HTTP status code is retryable
  against a *configurable* set of codes. Callers pass the authoritative set
  (the retry policy's ``retry_on_status_codes``); ``DEFAULT_RETRYABLE_STATUS``
  is the default used when no override is supplied.
- ``is_retryable`` walks an exception's ``__cause__`` / ``__context__`` chain
  looking for anything that presents as retryable via the ``Retryable``
  structural protocol. The walk is cycle-safe: a self-referential or looping
  cause chain terminates instead of spinning forever.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_RETRYABLE_STATUS",
    "Retryable",
    "is_retryable",
    "status_is_retryable",
]

#: Status codes retried by default: request timeout (408), rate limiting
#: (429), and the transient 5xx family (500/502/503/504). Permanent 5xx codes
#: — notably 501 (Not Implemented) and 505 (HTTP Version Not Supported) — are
#: excluded because retrying cannot change the outcome. This is only the
#: *default*; the retry policy's configurable ``retry_on_status_codes`` set is
#: the authority the classifier consults at call time.
DEFAULT_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})


@runtime_checkable
class Retryable(Protocol):
    """Structural opt-in for exception retryability.

    Any object exposing a boolean ``retryable`` attribute satisfies this
    protocol, so a non-SDK error can advertise whether retrying it is worth
    while without the classifier having to know its concrete type. ``HttpResponseError``
    satisfies it via its status-derived ``retryable`` flag.

    Attributes:
        retryable: Whether retrying the operation that raised this error might
            succeed.
    """

    retryable: bool


def status_is_retryable(status: int | None, retryable_statuses: Collection[int]) -> bool:
    """Return whether an HTTP status code is in the retryable set.

    Args:
        status: The HTTP status code, or ``None`` when no response was
            captured. ``None`` is never retryable.
        retryable_statuses: The authoritative set of codes to treat as
            retryable. Callers pass their configured set;
            ``DEFAULT_RETRYABLE_STATUS`` is the out-of-the-box default.

    Returns:
        ``True`` when ``status`` is present and a member of
        ``retryable_statuses``.
    """
    return status is not None and status in retryable_statuses


def is_retryable(error: BaseException) -> bool:
    """Return whether an exception (or its cause chain) is retryable.

    Walks ``error`` and, transitively, its ``__cause__`` (explicit
    ``raise ... from ...``) then ``__context__`` (implicit chaining) links,
    returning ``True`` as soon as a node presents as retryable via the
    ``Retryable`` protocol. The walk tracks visited exceptions by identity, so
    a self-referential or cyclic chain terminates rather than looping forever.

    Args:
        error: The exception to classify.

    Returns:
        ``True`` when ``error`` or any exception reachable through its cause
        chain is a ``Retryable`` with a truthy ``retryable`` flag.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, Retryable) and current.retryable:
            return True
        current = current.__cause__ or current.__context__
    return False
