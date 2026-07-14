# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Typed adapter over an SSE event stream.

`TypedSseConnection` / `AsyncTypedSseConnection` wrap any owning SSE source
(an `SseConnection`, or the free `parse_events` iterator paired with a closer)
and map each event through a caller-supplied callback. The mapper receives the
event name and the newline-joined data payload and returns either a mapped
value or an `SseSignal`:

- `SseSignal.SKIP` — discard this event and pull the next one.
- `SseSignal.DONE` — stop the stream (the wrapped source is released).

Mapping is lazy and pull-based: exactly one event is drawn from the underlying
source per unit of consumer demand (plus any events the mapper skips), never by
draining or buffering ahead. The wrapped source is released on every
termination path — clean exhaustion, a `DONE` signal, the consumer stopping
early, or the mapper raising — with the resource released before a mapper error
propagates.

This layer holds no sentinel, error, or serialisation conventions of its own:
what a data payload *means* (a ``[DONE]`` marker, a JSON document, a keep-alive)
is entirely the mapper's decision.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from types import TracebackType
    from typing import Self

    from .parser import SseEvent


class SseSignal(Enum):
    """Control signal a typed SSE mapper returns instead of a mapped value.

    Attributes:
        SKIP: Discard the current event and keep pulling from the stream.
        DONE: Stop the stream; the wrapped source is released.
    """

    SKIP = auto()
    DONE = auto()


#: A typed SSE mapper: ``(event_name, data) -> mapped value | SseSignal``.
type SseMapper[T] = Callable[[str, str], T | SseSignal]


class _SyncSseSource(Protocol):
    """An owning, iterable sync SSE source (structurally an `SseConnection`)."""

    def __iter__(self) -> Iterator[SseEvent]: ...

    def close(self) -> None: ...


class _AsyncSseSource(Protocol):
    """An owning, iterable async SSE source (an `AsyncSseConnection`)."""

    def __aiter__(self) -> AsyncIterator[SseEvent]: ...

    async def aclose(self) -> None: ...


class TypedSseConnection[T]:
    """Lazily maps a synchronous SSE source's events to typed values.

    Args:
        source: The owning SSE source to consume and release.
        mapper: Callback mapping ``(event_name, data)`` to a value, or to an
            `SseSignal` to skip the event or stop the stream.
    """

    __slots__ = ("_mapper", "_source")

    def __init__(self, source: _SyncSseSource, mapper: SseMapper[T]) -> None:
        self._source = source
        self._mapper = mapper

    def close(self) -> None:
        """Release the wrapped source. Idempotent (delegated to the source)."""
        self._source.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __iter__(self) -> Iterator[T]:
        """Yield mapped values lazily, honouring SKIP/DONE per consumer demand.

        Yields:
            Each event mapped to a non-signal value, in stream order.
        """
        try:
            for event in self._source:
                outcome = self._mapper(event.event, event.data)
                if isinstance(outcome, SseSignal):
                    if outcome is SseSignal.DONE:
                        return
                    continue  # SseSignal.SKIP: discard and pull the next event.
                yield outcome
        finally:
            # Runs before a mapper error propagates, so the resource is released
            # first; also covers DONE, a caller break, and clean exhaustion.
            self._source.close()


class AsyncTypedSseConnection[T]:
    """Asynchronous twin of `TypedSseConnection`.

    Args:
        source: The owning async SSE source to consume and release.
        mapper: Callback mapping ``(event_name, data)`` to a value, or to an
            `SseSignal` to skip the event or stop the stream.
    """

    __slots__ = ("_mapper", "_source")

    def __init__(self, source: _AsyncSseSource, mapper: SseMapper[T]) -> None:
        self._source = source
        self._mapper = mapper

    async def aclose(self) -> None:
        """Release the wrapped source. Idempotent (delegated to the source)."""
        await self._source.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def __aiter__(self) -> AsyncIterator[T]:
        """Yield mapped values lazily, honouring SKIP/DONE per consumer demand.

        Yields:
            Each event mapped to a non-signal value, in stream order.
        """
        try:
            async for event in self._source:
                outcome = self._mapper(event.event, event.data)
                if isinstance(outcome, SseSignal):
                    if outcome is SseSignal.DONE:
                        return
                    continue  # SseSignal.SKIP: discard and pull the next event.
                yield outcome
        finally:
            await self._source.aclose()


__all__ = [
    "AsyncTypedSseConnection",
    "SseMapper",
    "SseSignal",
    "TypedSseConnection",
]
