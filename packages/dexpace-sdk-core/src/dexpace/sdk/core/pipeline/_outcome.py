# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""``Success | Failure`` outcome sum type shared by the recovery folds.

The recovery machinery models the result of running a request through the
pipeline as a two-case sum: a ``Success`` carrying an (open) response, or a
``Failure`` carrying the converted exception. Only ``Exception`` ever folds
into a ``Failure`` — a ``BaseException`` such as ``asyncio.CancelledError`` or
``KeyboardInterrupt`` is never converted (see ``_recovery`` / ``_async_recovery``).

The sum type is I/O-free, so the sync and async folds share one definition of
the shape and of the identity-preserving ``unwrap``. Response-type specialised
folding (closing a held response, buffering an error body) lives in the
side-specific modules because those steps differ between ``Response`` and
``AsyncResponse``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Success[R]:
    """Outcome carrying a produced response.

    Attributes:
        value: The response the pipeline produced. Held open — the recovery
            fold only closes it when a later step fails.
    """

    value: R


@dataclass(frozen=True, slots=True)
class Failure:
    """Outcome carrying a converted exception.

    Attributes:
        error: The original ``Exception`` instance that was folded in. Held by
            identity so ``unwrap`` can re-raise the exact object a caller
            expects — never a copy or a wrapper.
    """

    error: Exception


#: A pipeline result: either a produced response or a converted exception.
type Outcome[R] = Success[R] | Failure


def unwrap[R](outcome: Outcome[R]) -> R:
    """Collapse an outcome to its response, re-raising a ``Failure``'s error.

    Identity-preserving: the exact exception instance a step raised (and that
    was folded into the ``Failure``) is re-raised unchanged, with its original
    traceback, so a caller's ``except`` clause receives the original object
    rather than a reconstructed one.

    Args:
        outcome: The outcome to collapse.

    Returns:
        The response held by a ``Success``.

    Raises:
        Exception: The original error held by a ``Failure``.
    """
    if isinstance(outcome, Success):
        return outcome.value
    raise outcome.error


__all__ = ["Failure", "Outcome", "Success", "unwrap"]
