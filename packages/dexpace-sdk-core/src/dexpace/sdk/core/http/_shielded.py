# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Cancellation-shielded cleanup shared by the async request/response bodies.

Both the request and response sides release transport resources from a
``finally`` block that may run while an ``asyncio.CancelledError`` is already
propagating. ``_shielded_cleanup`` is the single convention they share to run
that release to completion before letting the cancellation continue. It lives
here — under ``http`` rather than ``http.response`` — so the request-side body
can import it without reaching into the response package and recreating the
import cycle between the two body modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def _shielded_cleanup(cleanup: Awaitable[object]) -> None:
    """Run a cleanup coroutine without letting cancellation interrupt it.

    This is the single cancellation convention used by the async request and
    response bodies: a ``finally`` block that releases transport resources may
    run while an ``asyncio.CancelledError`` is already propagating through the
    enclosing task. Awaiting the cleanup directly would let that cancellation
    interrupt it mid-way, leaking the underlying connection.

    The cleanup is wrapped in ``asyncio.shield`` so it always runs to
    completion. If the surrounding scope is cancelled, the ``CancelledError``
    raised by ``shield`` is caught and the wait retried until the shielded
    cleanup finishes; the cancellation is then re-raised so it continues to
    propagate. Cleanup never swallows cancellation — it merely defers it
    until the resource is released. A ``CancelledError`` raised because the
    cleanup *itself* was cancelled is propagated immediately.

    A pending outer cancellation always wins: if the cleanup runs to
    completion but raises an ordinary exception while a cancellation is
    waiting, the cancellation is re-raised (the cleanup error does not mask
    it). When no cancellation is pending, a cleanup failure surfaces to the
    caller unchanged.

    Args:
        cleanup: The resource-release coroutine to run to completion.

    Raises:
        asyncio.CancelledError: Re-raised after the cleanup completes when
            the enclosing scope was cancelled while the cleanup ran.
        Exception: Whatever the cleanup coroutine raised, when no outer
            cancellation is pending.
    """
    inner = asyncio.ensure_future(cleanup)
    cancelled = False
    while not inner.done():
        try:
            await asyncio.shield(inner)
        except asyncio.CancelledError:
            if inner.cancelled():
                # The cleanup itself was cancelled, not just our wait on it.
                raise
            # An outer cancellation hit our wait, not the shielded cleanup.
            # Keep waiting until the cleanup finishes, then re-raise so the
            # cancellation continues to propagate.
            cancelled = True
        except Exception:
            # The cleanup failed; ``inner`` retains the exception, surfaced
            # below. A pending cancellation still takes precedence.
            break
    if cancelled:
        raise asyncio.CancelledError
    inner.result()


__all__ = ["_shielded_cleanup"]
