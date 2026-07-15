# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Lock-protected single-use guard shared by the stream/iter-backed bodies.

The single-use request and response bodies (stream- and iter-backed) may be
consumed exactly once: a second attempt to iterate them raises ``RuntimeError``.
That "already consumed?" decision is a check-then-act on a shared flag, so a
bare ``bool`` is not enough — two threads racing the very first consumption
could each read ``False`` before either writes ``True`` and both proceed,
consuming the underlying stream twice. This SDK targets free-threaded CPython
(PEP 703), where that race is real rather than masked by the GIL, so the flag
lives behind a ``threading.Lock``: the check and the set happen atomically under
the lock, guaranteeing exactly one winner regardless of interpreter build.

The guard is deliberately synchronous. The async bodies claim it from their
(non-``async``) ``aiter_bytes`` entry point, before returning the iterator, so
the lock is only ever held for the trivial check-and-set and never across an
``await`` — it cannot block an event loop or deadlock. The same ``threading``
lock therefore also protects a sync body driven through a sync/async bridge
(for example a thread-pool executor), since the guarantee never depended on the
GIL in the first place.

It lives here — under ``http`` rather than ``http.request`` or ``http.response``
— so both body sides import it without one body package reaching into the other
(mirroring how ``_shielded`` is shared).
"""

from __future__ import annotations

import threading


class _OnceGuard:
    """A thread-safe, single-claim guard for single-use body consumption.

    Exposes one operation, ``claim``: the first caller wins and every later
    caller raises ``RuntimeError``. The winner/loser decision is made under a
    ``threading.Lock``, so it holds without relying on GIL atomicity of the
    flag read/write — N threads racing the very first ``claim`` yield exactly
    one winner.
    """

    __slots__ = ("_claimed", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed = False

    def claim(self, message: str) -> None:
        """Atomically claim the single consumption, or raise if already claimed.

        The check-then-act runs entirely inside the lock, so the flag is never
        read and written across a window another thread could interleave into.

        Args:
            message: The ``RuntimeError`` message to raise when the single
                consumption has already been claimed by an earlier caller.

        Raises:
            RuntimeError: If a previous caller already claimed consumption.
        """
        with self._lock:
            if self._claimed:
                raise RuntimeError(message)
            self._claimed = True


__all__ = ["_OnceGuard"]
