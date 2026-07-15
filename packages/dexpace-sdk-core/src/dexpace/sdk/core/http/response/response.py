# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Immutable HTTP response model."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import TYPE_CHECKING, Self, cast

from ..common.headers import Headers
from ..common.http_header_name import HttpHeaderName
from ..common.protocol import Protocol
from .status import Status

if TYPE_CHECKING:
    from ...serde import Serde
    from ..request.request import Request
    from .response_body import ResponseBody

type _Name = str | HttpHeaderName


class _Outcome:
    """A memoized parse result: either a value or the exception it raised.

    Wrapping both possibilities in one object lets the latch publish the outcome
    with a single attribute store while still distinguishing "not computed yet"
    (the cell holds ``None``) from a legitimately parsed ``None`` value (the cell
    holds an `_Outcome` whose ``_value`` is ``None``).
    """

    __slots__ = ("_error", "_value")

    def __init__(self, value: object, error: Exception | None) -> None:
        self._value = value
        self._error = error

    @classmethod
    def capture(cls, factory: Callable[[], object]) -> _Outcome:
        """Run ``factory`` once, capturing its value or the exception it raises."""
        try:
            return cls(factory(), None)
        except Exception as exc:
            # Any decode/handler failure is stored verbatim and re-raised on
            # every later access; control-flow exceptions (KeyboardInterrupt,
            # SystemExit) are not ``Exception`` and propagate uncached.
            return cls(None, exc)

    def unwrap(self) -> object:
        """Return the captured value, or re-raise the captured failure."""
        if self._error is not None:
            raise self._error
        return self._value


class _ParseLatch:
    """Double-checked memoization cell backing `Response.parse`.

    The decoded outcome — a value *or* a thrown failure — is computed at most
    once and then served to every later (and concurrent) caller. Correctness
    does not rest on any atomicity of the fast-path read: a racing reader may
    observe the cell as empty and fall through to the lock, but only the caller
    that wins the lock ever runs the factory, and the re-check *inside* the lock
    closes the window. Publishing the outcome is a single attribute store, so a
    fast-path reader observes either ``None`` or the fully built `_Outcome` —
    never a half-initialized one. This holds on free-threaded CPython (PEP 703),
    where the GIL no longer serializes the check-then-act, exactly as it does
    under the GIL.

    Both a successful result (including ``None``) and a thrown failure are
    memoized: a later `Response.parse` after a decode failure re-raises the same
    error without re-running the factory or re-reading the single-use body.
    """

    __slots__ = ("_lock", "_outcome")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outcome: _Outcome | None = None

    def get(self, factory: Callable[[], object]) -> object:
        """Return the memoized value, computing it via ``factory`` exactly once.

        Args:
            factory: Zero-arg callable producing the value on first access. It
                runs at most once, under the lock, even across concurrent first
                calls; whether it returns or raises, the outcome is cached and
                every later call replays it (re-raising the same failure).

        Returns:
            The memoized value.

        Raises:
            Exception: The same exception the factory raised on first access,
                re-raised on every later call.
        """
        outcome = self._outcome
        if outcome is not None:
            return outcome.unwrap()
        with self._lock:
            outcome = self._outcome
            if outcome is not None:
                return outcome.unwrap()
            outcome = _Outcome.capture(factory)
            self._outcome = outcome
            return outcome.unwrap()


@dataclass(frozen=True, slots=True)
class Response:
    """Immutable HTTP response produced by a transport.

    Implements the context-manager protocol so callers can

    .. code-block:: python

        with http_client.execute(request) as response:
            ...

    and the underlying body is closed deterministically on exit. The header /
    metadata surface is immutable and safe to share across threads; the body,
    when present, carries single-use stream state — see `ResponseBody`.

    Deserialize the body with `parse`, which reads and decodes it exactly once
    and memoizes the result for repeat (and concurrent) callers.
    """

    request: Request
    protocol: Protocol
    status: Status
    headers: Headers = field(default_factory=Headers)
    reason: str | None = None
    body: ResponseBody | None = None
    _latch: _ParseLatch = field(default_factory=_ParseLatch, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate the required exchange fields.

        Raises:
            ValueError: If ``request``, ``protocol``, or ``status`` is missing.
                The message names the offending field.
        """
        if self.request is None:
            raise ValueError("request is required")
        if self.protocol is None:
            raise ValueError("protocol is required")
        if self.status is None:
            raise ValueError("status is required")

    def __repr__(self) -> str:
        """Return a repr that never echoes credentials.

        The wrapped ``request`` renders through `Request.__repr__` (which routes
        its URL through the shared ``UrlRedactor``) and ``headers`` renders
        through `Headers` (which masks credential-bearing values), so neither
        URL userinfo / query secrets nor authorization headers leak. The private
        parse latch is omitted.
        """
        return (
            f"Response(request={self.request!r}, protocol={self.protocol!r}, "
            f"status={self.status!r}, headers={self.headers!r}, "
            f"reason={self.reason!r}, body={self.body!r})"
        )

    def parse[T](self, target: type[T], *, serde: Serde | None = None) -> T:
        """Decode the response body into ``target``, once, and memoize it.

        The body is read and deserialized on the first call — a single-use
        operation, since reading a body consumes it — and the decoded value is
        cached. Every later call, including one racing the first from another
        thread, returns that same cached object without touching the body. The
        memoization is latched under an explicit lock with a double-checked
        re-check, so exactly one deserialization happens even under concurrent
        first access on free-threaded CPython. A different ``target`` on a later
        call is ignored: the body is already gone, so the first result stands.

        Args:
            target: The type to reconstruct — a dataclass, ``list[X]`` /
                ``dict[str, X]``, a scalar, or any other type the codec accepts.
                A subscripted generic such as ``list[Pet]`` may be passed
                directly.
            serde: Wire-format decoder for the raw bytes. Defaults to JSON.

        Returns:
            The decoded value.

        Raises:
            ValueError: If the response has no body to parse.
        """
        return cast("T", self._latch.get(lambda: self._decode(target, serde)))

    def _decode[T](self, target: type[T], serde: Serde | None) -> T:
        """Read the body fully and decode it into ``target`` (runs once)."""
        if self.body is None:
            raise ValueError("response has no body to parse")
        from ...serde import JSON_SERDE, Codec

        active = serde if serde is not None else JSON_SERDE
        structure = active.deserializer.deserialize_bytes(self.body.bytes())
        return Codec().decode(structure, target)

    def close(self) -> None:
        """Close the response body. Idempotent."""
        if self.body is not None:
            self.body.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def is_success(self) -> bool:
        return self.status.is_success

    @property
    def is_redirect(self) -> bool:
        return self.status.is_redirect

    @property
    def is_client_error(self) -> bool:
        return self.status.is_client_error

    @property
    def is_server_error(self) -> bool:
        return self.status.is_server_error

    def with_status(self, status: Status) -> Self:
        return replace(self, status=status)

    def with_headers(self, headers: Headers) -> Self:
        return replace(self, headers=headers)

    def with_body(self, body: ResponseBody | None) -> Self:
        return replace(self, body=body)

    def with_header(self, name: _Name, value: str) -> Self:
        return replace(self, headers=self.headers.with_set(name, value))


__all__ = ["Response"]
