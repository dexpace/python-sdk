# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Reconnecting Server-Sent Events client built on the sans-io parser.

``SseConnection`` / ``AsyncSseConnection`` drive a long-lived SSE stream
through the pipeline and transparently reconnect when it drops, replaying the
``Last-Event-ID`` header and honouring the server's ``retry:`` hint with
jittered backoff. Parsing is delegated to the existing sans-io parser; this
layer owns only the connection lifecycle.

Semantics follow the browser ``EventSource``: a reconnect is attempted on both
a transient transport error and a clean end-of-stream, while a non-success HTTP
status is a permanent failure (raised, never retried). The caller ends the
stream by breaking the loop (for example on a ``data: [DONE]`` sentinel).
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, cast

from ...errors import HttpResponseError, ServiceRequestError, ServiceResponseError
from ...pipeline.dispatch import (
    AsyncPipelineLike,
    SendAsync,
    SendSync,
    SyncPipelineLike,
)
from ...util.clock import ASYNC_SYSTEM_CLOCK, SYSTEM_CLOCK, AsyncClock, Clock
from ..context.dispatch_context import DispatchContext
from ..response.async_response_body import _shielded_cleanup
from .parser import SseParser, parse_async_events

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Generator, Iterator
    from types import TracebackType
    from typing import Self

    from ..request.request import Request
    from ..response.async_response import AsyncResponse
    from ..response.response import Response
    from .parser import SseEvent

_LAST_EVENT_ID: str = "Last-Event-ID"
_DEFAULT_RETRY_SECONDS: float = 3.0
_DEFAULT_MAX_BACKOFF: float = 30.0
_DEFAULT_JITTER: float = 0.1

#: Transient failures that trigger a reconnect rather than propagating. A
#: clean end-of-stream reconnects too (handled separately); anything outside
#: this set — including ``CancelledError`` and ``HttpResponseError`` — is fatal.
_TRANSIENT: tuple[type[BaseException], ...] = (
    ServiceRequestError,
    ServiceResponseError,
    OSError,
)


def _resume_request(initial: Request, last_event_id: str | None) -> Request:
    """Return ``initial``, stamping ``Last-Event-ID`` when an id is known."""
    if last_event_id:
        return initial.with_header(_LAST_EVENT_ID, last_event_id)
    return initial


def _last_event_id_of(event: SseEvent, current: str | None) -> str | None:
    """Return the id to resume from after ``event``.

    An explicit empty id clears the stored id (per the SSE spec), so the next
    reconnect omits the ``Last-Event-ID`` header.
    """
    if event.id is not None:
        return event.id or None
    return current


def _next_backoff(
    retry_base: float, failures: int, max_backoff: float, jitter: float, rand: random.Random
) -> float:
    """Exponential backoff with an upward-only jitter, capped at ``max_backoff``.

    Jitter only lengthens the wait, so a fleet de-synchronises without any
    client reconnecting sooner than the computed delay.
    """
    bounded = min(max_backoff, retry_base * (2**failures))
    # cast required: random.Random.uniform is typed as returning Any in typeshed,
    # so mypy raises [no-any-return] without it.
    return cast(float, bounded * rand.uniform(1.0, 1.0 + jitter))


class SseConnection:
    """Synchronous reconnecting SSE stream.

    Iterate it (directly or via ``with``) to receive ``SseEvent``s across
    reconnections. Each (re)connection is dispatched through ``source`` — a
    ``Pipeline`` (run with a fresh dispatch context per connection) or a bare
    ``Request -> Response`` callable — so retry, auth, and tracing apply per
    connection.

    Args:
        source: A sync pipeline or a send-callable.
        initial_request: The request opening the stream; reused for every
            reconnection with an updated ``Last-Event-ID``.
        last_event_id: Seed id to resume a previously-interrupted stream.
        default_retry: Backoff base (seconds) used until the server sends a
            ``retry:`` value.
        max_backoff: Ceiling on any single reconnect delay.
        max_reconnects: Maximum consecutive failed reconnections before
            iteration raises. ``None`` reconnects indefinitely.
        jitter: Upward jitter fraction applied to the backoff.
        clock: Time source for backoff sleeps (injected for tests).
        rand: RNG for jitter (injected for tests).
        dispatch_factory: Builds the dispatch context per connection when
            ``source`` is a pipeline. Defaults to ``DispatchContext.noop``.
    """

    __slots__ = (
        "_clock",
        "_dispatch_factory",
        "_initial",
        "_jitter",
        "_last_event_id",
        "_max_backoff",
        "_max_reconnects",
        "_rand",
        "_response",
        "_retry_base",
        "_send",
    )

    def __init__(
        self,
        source: SyncPipelineLike | SendSync,
        initial_request: Request,
        *,
        last_event_id: str | None = None,
        default_retry: float = _DEFAULT_RETRY_SECONDS,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        max_reconnects: int | None = None,
        jitter: float = _DEFAULT_JITTER,
        clock: Clock = SYSTEM_CLOCK,
        rand: random.Random | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        self._initial = initial_request
        self._last_event_id = last_event_id
        self._retry_base = default_retry
        self._max_backoff = max_backoff
        self._max_reconnects = max_reconnects
        self._jitter = jitter
        self._clock = clock
        self._rand = rand if rand is not None else random.Random()
        self._dispatch_factory = dispatch_factory or DispatchContext.noop
        self._response: Response | None = None
        self._send = self._normalise(source)

    def _normalise(self, source: SyncPipelineLike | SendSync) -> SendSync:
        if isinstance(source, SyncPipelineLike):
            pipeline = source

            def send(request: Request) -> Response:
                return pipeline.run(request, self._dispatch_factory())

            return send
        return source

    def close(self) -> None:
        """Close the current response, if any. Idempotent."""
        response = self._response
        if response is None:
            return
        self._response = None
        response.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __iter__(self) -> Iterator[SseEvent]:
        failures = 0
        last_error: BaseException | None = None
        try:
            while True:
                response = self._connect(_resume_request(self._initial, self._last_event_id))
                progressed, error = yield from self._stream(response)
                if error is not None:
                    last_error = error
                self.close()
                if progressed:
                    failures = 0
                if self._max_reconnects is not None and failures >= self._max_reconnects:
                    raise ServiceResponseError("SSE reconnect budget exhausted") from last_error
                self._clock.sleep(
                    _next_backoff(
                        self._retry_base, failures, self._max_backoff, self._jitter, self._rand
                    )
                )
                failures += 1
        finally:
            self.close()

    def _connect(self, request: Request) -> Response:
        response = self._send(request)
        self._response = response
        if not response.status.is_success:
            self.close()
            raise HttpResponseError(response=response)
        return response

    def _stream(
        self, response: Response
    ) -> Generator[SseEvent, None, tuple[bool, BaseException | None]]:
        """Yield one connection's events; return (progressed, transient_error).

        A transient transport error mid-stream ends the generator normally (so
        the caller reconnects) and is returned so the caller can chain it as the
        cause if the reconnect budget is later exhausted; other exceptions
        propagate. A ``retry:`` hint that arrived without a data event is picked
        up from the parser's sticky state after iteration.
        """
        progressed = False
        error: BaseException | None = None
        body = response.body
        if body is None:
            return progressed, error
        parser = SseParser()
        try:
            for chunk in body.iter_bytes():
                parser.feed(chunk)
                for event in parser.drain():
                    self._last_event_id = _last_event_id_of(event, self._last_event_id)
                    progressed = True
                    yield event
            for event in parser.end():
                self._last_event_id = _last_event_id_of(event, self._last_event_id)
                progressed = True
                yield event
        except _TRANSIENT as exc:
            error = exc
        if parser.retry is not None:
            self._retry_base = parser.retry / 1000.0
        return progressed, error


class AsyncSseConnection:
    """Asynchronous twin of :class:`SseConnection`.

    Mirrors the sync client exactly with ``async`` iteration semantics. Each
    (re)connection is dispatched through ``source`` — an ``AsyncPipeline`` (run
    with a fresh dispatch context per connection) or an async
    ``Request -> AsyncResponse`` callable. The response is closed through the
    shielded-cleanup convention, so a cancelled consumer still releases the
    transport handle before the cancellation continues to propagate.

    Args:
        source: An async pipeline or an async send-callable.
        initial_request: The request opening the stream; reused for every
            reconnection with an updated ``Last-Event-ID``.
        last_event_id: Seed id to resume a previously-interrupted stream.
        default_retry: Backoff base (seconds) used until the server sends a
            ``retry:`` value.
        max_backoff: Ceiling on any single reconnect delay.
        max_reconnects: Maximum consecutive failed reconnections before
            iteration raises. ``None`` reconnects indefinitely.
        jitter: Upward jitter fraction applied to the backoff.
        clock: Async time source for backoff sleeps (injected for tests).
        rand: RNG for jitter (injected for tests).
        dispatch_factory: Builds the dispatch context per connection when
            ``source`` is a pipeline. Defaults to ``DispatchContext.noop``.
    """

    __slots__ = (
        "_clock",
        "_dispatch_factory",
        "_initial",
        "_jitter",
        "_last_event_id",
        "_max_backoff",
        "_max_reconnects",
        "_rand",
        "_response",
        "_retry_base",
        "_send",
    )

    def __init__(
        self,
        source: AsyncPipelineLike | SendAsync,
        initial_request: Request,
        *,
        last_event_id: str | None = None,
        default_retry: float = _DEFAULT_RETRY_SECONDS,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        max_reconnects: int | None = None,
        jitter: float = _DEFAULT_JITTER,
        clock: AsyncClock = ASYNC_SYSTEM_CLOCK,
        rand: random.Random | None = None,
        dispatch_factory: Callable[[], DispatchContext] | None = None,
    ) -> None:
        self._initial = initial_request
        self._last_event_id = last_event_id
        self._retry_base = default_retry
        self._max_backoff = max_backoff
        self._max_reconnects = max_reconnects
        self._jitter = jitter
        self._clock = clock
        self._rand = rand if rand is not None else random.Random()
        self._dispatch_factory = dispatch_factory or DispatchContext.noop
        self._response: AsyncResponse | None = None
        self._send = self._normalise(source)

    def _normalise(self, source: AsyncPipelineLike | SendAsync) -> SendAsync:
        if isinstance(source, AsyncPipelineLike):
            pipeline = source

            async def send(request: Request) -> AsyncResponse:
                return await pipeline.run(request, self._dispatch_factory())

            return send
        return source

    async def aclose(self) -> None:
        """Close the current response, if any. Idempotent and cancel-safe."""
        response = self._response
        if response is None:
            return
        self._response = None
        await _shielded_cleanup(response.close())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __aiter__(self) -> AsyncIterator[SseEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[SseEvent]:
        failures = 0
        last_error: BaseException | None = None
        try:
            while True:
                response = await self._connect(_resume_request(self._initial, self._last_event_id))
                progressed = False
                body = response.body
                if body is not None:
                    stream = parse_async_events(body.aiter_bytes())
                    try:
                        async with stream:
                            async for event in stream:
                                self._last_event_id = _last_event_id_of(event, self._last_event_id)
                                progressed = True
                                yield event
                    except _TRANSIENT as exc:
                        last_error = exc
                    if stream.retry is not None:
                        self._retry_base = stream.retry / 1000.0
                await self.aclose()
                if progressed:
                    failures = 0
                if self._max_reconnects is not None and failures >= self._max_reconnects:
                    raise ServiceResponseError("SSE reconnect budget exhausted") from last_error
                await self._clock.sleep(
                    _next_backoff(
                        self._retry_base, failures, self._max_backoff, self._jitter, self._rand
                    )
                )
                failures += 1
        finally:
            await self.aclose()

    async def _connect(self, request: Request) -> AsyncResponse:
        response = await self._send(request)
        self._response = response
        if not response.status.is_success:
            await self.aclose()
            raise HttpResponseError(response=response)
        return response


__all__ = ["AsyncSseConnection", "SseConnection"]
